"""HIPAA Safe Harbor de-identification pipeline for PHI.

Detects and removes protected health information identifiers before content
is sent to an external LLM or stored outside the trust boundary.

Covers all 18 HIPAA Safe Harbor identifier classes that surface in
unstructured clinical text — the 10 original clinical categories (patient
name, MRN, DOB, phone, email, SSN, address, medical record, dates, IP
address) plus 8 additional classes: fax numbers, account numbers, license
numbers, vehicle identifiers, device identifiers, URLs, biometric
identifiers and full-face photo references. (The remaining Safe Harbor
classes — certificate/license numbers for the *provider* and sub-state
geography — are governed by the upstream :mod:`doctoragent.security.dlp`
scanner; the geographic subset is intentionally not redacted here to
preserve clinical locality context.)

Detection reuses :mod:`doctoragent.security.dlp`'s regex catalogue (phone,
email, SSN, IP) so this module stays consistent with the existing DLP
scanner; medical-context identifiers (MRN, DOB, dates, patient-name
heuristics, street addresses) and the 8 new identifier classes are added
here. Three de-identification strategies are supported:

* ``redact``       — replace each match with ``[REDACTED]``.
* ``pseudonymize`` — replace each unique value with a stable, type-tagged
  placeholder (``[PHONE_8f3a1b2c]``) and return a reversible mapping so the
  caller can restore originals in a trusted context. Placeholders are
  derived from SHA-256(salt + value) so they are stable yet non-reversible
  without the mapping.
* ``mask``         — partially mask each match, preserving a minimal hint
  (last 4 digits of a phone, first initial of a name, …).

Name detection is intentionally regex/heuristic-based rather than depending
on a heavy NER model. In addition to the original clinical-title prefix
(``Dr.``/``Mr.``/``Patient`` …) it now also catches:

* English context phrases — ``patient named X``, ``the patient, X``,
  ``seen by X``.
* Bare two-capitalised-token English names (``John Doe``), excluding
  common sentence-initial and place/organisational words.
* Chinese context-triggered names — ``患者/病人/产妇/患儿/姓名`` followed
  by a surname-led 2-3 character name.
* Chinese surname + title-suffix names — ``张三先生`` / ``李四女士``.

A spaCy/stanza NER model would still raise recall further; the heuristic
layer keeps the install footprint small while plugging the most common
title-less blind spots.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

from doctoragent.compat import StrEnum
from doctoragent.security.dlp import _BUILTIN_PATTERNS, SensitiveDataType

__all__ = ["PHIDetector", "PHIType"]


class PHIType(StrEnum):
    """PHI identifier categories covered by the Safe Harbor method.

    The first ten members are the original clinical subset; the remaining
    eight (``FAX`` … ``FULL_FACE``) extend coverage toward the full Safe
    Harbor 18-identifier list.
    """

    PATIENT_NAME = "PATIENT_NAME"
    MRN = "MRN"
    DOB = "DOB"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    SSN = "SSN"
    ADDRESS = "ADDRESS"
    MEDICAL_RECORD = "MEDICAL_RECORD"
    DATE = "DATE"
    IP_ADDRESS = "IP_ADDRESS"
    # —— 新增 8 类（补齐 HIPAA Safe Harbor 缺失项）——
    FAX = "FAX"  # 传真号码
    ACCOUNT_NUMBER = "ACCOUNT_NUMBER"  # 账号
    LICENSE_NUMBER = "LICENSE_NUMBER"  # 执照 / 许可证号
    VEHICLE_ID = "VEHICLE_ID"  # 车辆标识 (VIN / 车牌)
    DEVICE_ID = "DEVICE_ID"  # 设备 / 序列号
    URL = "URL"  # 统一资源定位符
    BIO_METRIC = "BIO_METRIC"  # 生物标识
    FULL_FACE = "FULL_FACE"  # 面部照片引用
    ID_CARD = "ID_CARD"  # 中国大陆18位身份证号


# ── Detection patterns ──────────────────────────────────────────────────────
#
# ``_BUILTIN_PATTERNS`` (from doctoragent.security.dlp) supplies canonical
# regexes for PHONE, EMAIL, SSN and IP_ADDRESS. We compile a working copy
# here and add the medical-context identifiers on top.

_DLP_REUSE: dict[PHIType, str] = {
    PHIType.PHONE: _BUILTIN_PATTERNS[SensitiveDataType.PHONE],
    PHIType.EMAIL: _BUILTIN_PATTERNS[SensitiveDataType.EMAIL],
    PHIType.SSN: _BUILTIN_PATTERNS[SensitiveDataType.SSN],
    PHIType.IP_ADDRESS: _BUILTIN_PATTERNS[SensitiveDataType.IP_ADDRESS],
}

# 常见中文姓氏（用于姓名启发式检测，覆盖百家姓中高频姓氏）。
_CJK_SURNAMES: str = (
    "王李张刘陈杨黄赵周吴徐孙朱马胡郭林何高梁郑罗宋谢唐韩曹许邓萧冯曾程蔡彭潘"
    "袁于董余苏叶吕魏蒋田杜丁沈姜范江傅钟卢汪戴崔任陆廖姚方金邱夏谭韦贾邹石熊"
    "孟秦阎薛侯雷白龙段郝孔邵史毛常万顾赖武康贺严尹钱施牛洪龚"
)

# FAX 后处理用关键词：电话号码附近出现这些词时改判为 FAX。
_FAX_KEYWORD_RE: re.Pattern[str] = re.compile(r"(?i)\b(?:fax|传真)\b")

# pseudonymize 默认盐值（可用 config["pseudonym_salt"] 覆盖）。
_DEFAULT_PSEUDONYM_SALT: str = "doctoragent-phi-v1"

logger = logging.getLogger(__name__)

# ── Optional NER layer (v0.3.19) ────────────────────────────────────────
# Regex/heuristic name detection cannot reach HIPAA Safe Harbor recall on its
# own. When a spaCy model is available (``pip install spacy`` + a ``PER``-
# capable model such as zh_core_web_sm / en_core_web_sm), PERSON entities are
# merged into the PATIENT_NAME candidate set; the existing overlap dedupe
# arbitrates against regex hits.
#
# Enable via config key ``spacy_model`` or env
# ``DOCTORAGENT_SECURITY__DEID_SPACY_MODEL`` (model name; empty/unset = off).
_NER_NLP: Any = None
_NER_INIT_DONE = False


def _get_ner_pipeline(model_name: str | None) -> Any:
    """Return a cached spaCy pipeline, or ``None`` when unavailable/disabled.

    Failure is latched for the process lifetime (one warning) so a broken
    model never turns every detection call into a retry storm.
    """
    global _NER_NLP, _NER_INIT_DONE
    if not model_name:
        return None
    if _NER_INIT_DONE:
        return _NER_NLP
    try:
        import spacy  # type: ignore[import-not-found]

        _NER_NLP = spacy.load(model_name, exclude=["parser", "lemmatizer"])
        logger.info("PHI de-identification NER enabled (model=%s)", model_name)
    except Exception as exc:  # noqa: BLE001 — degrade once, then stay off
        logger.warning(
            "spaCy NER unavailable (model=%r): %s — falling back to regex-only "
            "name detection",
            model_name,
            exc,
        )
        _NER_NLP = None
    _NER_INIT_DONE = True
    return _NER_NLP


def _reset_ner_cache() -> None:
    """Test hook: clear the cached pipeline so env changes are re-read."""
    global _NER_NLP, _NER_INIT_DONE
    _NER_NLP = None
    _NER_INIT_DONE = False

# 英文双词姓名排除集：句首常见词、地名、机构词，避免把 “San Francisco”
# / “Emergency Room” 之类误判为人名。
_ENGLISH_NAME_EXCLUDES: str = (
    "The|This|That|These|Those|There|Here|When|Then|After|Before|Please|Thank|"
    "Dear|Your|What|Where|Why|Who|Which|How|Now|Also|However|Although|Because|"
    "While|During|About|Above|Below|Between|Within|Without|Patient|Hospital|"
    "Clinic|Department|University|Medical|General|Internal|Family|Community|"
    "Public|Private|North|South|East|West|Central|Northern|Southern|Eastern|"
    "Western|New|San|Los|Las|United|States|State|City|County|District|Region|"
    "Area|Service|Center|Centre|Institute|College|School|Association|Society|"
    "Foundation|Group|Team|Office|Bureau|Agency|Authority|Council|Assembly|"
    "Court|House|Home|Room|Ward|Bed|Unit|Floor|Building|Tower|Block|Chief|"
    "Senior|Junior|Major|Minor|Primary|Secondary|Acute|Chronic|Emergency|"
    "Outpatient|Inpatient|Discharge|Admission|Intake|Transfer|Referral"
)

# 姓名检测复合模式：在原有临床称谓前缀基础上，增加无前缀的启发式检测。
_PATIENT_NAME_PATTERN: str = (
    # 1) 临床称谓前缀 + 英文姓名（原有逻辑，保留）
    r"\b(?:Dr\.?|Mr\.?|Ms\.?|Mrs\.?|Patient)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"
    # 2) 英文上下文短语（关键词大小写不敏感，姓名仍要求首字母大写）
    r"|(?i:patient\s+named|the\s+patient,?|seen\s+by)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"
    # 3) 英文双 capitalized 词姓名，排除句首/地名/机构常见词
    rf"|\b(?!{_ENGLISH_NAME_EXCLUDES})[A-Z][a-z]+\s+[A-Z][a-z]+\b"
    # 4) 中文上下文关键词 + 姓氏开头的 2-3 字姓名（后接非 CJK 避免截断长词）
    rf"|(?:患者|病人|产妇|患儿|姓名)\s*[{_CJK_SURNAMES}][\u4e00-\u9fa5]{{1,2}}(?![\u4e00-\u9fa5])"
    # 5) 中文姓氏姓名 + 称谓后缀
    rf"|[{_CJK_SURNAMES}][\u4e00-\u9fa5]{{1,2}}(?:先生|女士|同志|医生|大夫|主任|护士|教授)"
)

# Medical-context patterns. Order matters only for human readability; the
# detector de-duplicates overlapping matches by span + length. FAX is *not*
# listed here — it is derived from PHONE matches via post-processing in
# :meth:`PHIDetector.detect_phi` (a phone number near a "fax"/"传真" keyword
# is re-tagged as FAX).
_MEDICAL_PATTERNS: dict[PHIType, str] = {
    # Date of birth — anchored on a "DOB"/"Date of Birth" label so we can
    # tag it as DOB rather than a generic DATE.
    PHIType.DOB: (
        r"(?:\bDOB\b|\bDate of Birth\b|\bBorn\b)\s*[:\-]?\s*"
        r"\d{4}-\d{1,2}-\d{1,2}"
        r"|\b(?:DOB|Date of Birth|Born)\s*[:\-]?\s*"
        r"\d{1,2}/\d{1,2}/\d{2,4}"
    ),
    # Generic dates — ISO, slash, and "Jan 5, 2024" style.
    PHIType.DATE: (
        r"\b\d{4}-\d{1,2}-\d{1,2}\b"
        r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
        r"|\b\d{1,2}\s+"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
        r",?\s+\d{4}\b"
    ),
    # 病历号 — 严格模式：必须有 MRN/medical record/病历号/档案号/住院号
    # 等标签前缀（去重时优先于宽松的 MRN 裸数字匹配）。
    PHIType.MEDICAL_RECORD: (
        r"\b(?:Medical Record(?: Number)?|MRN|Record #|病历号|档案号|住院号)"
        r"\s*[:#]?\s*\d{6,12}\b"
    ),
    # MRN — 宽松模式（低置信度）：仅匹配裸 7-10 位数字。带标签的情况由
    # 上面的 MEDICAL_RECORD 模式优先匹配（去重时更长 span 胜出）。
    PHIType.MRN: r"\b\d{7,10}\b",
    # Street address — number + street name + suffix.
    PHIType.ADDRESS: (
        r"\b\d{1,6}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?\s+"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|"
        r"Way|Court|Ct|Place|Pl)\b\.?"
    ),
    # 患者姓名 — 见 _PATIENT_NAME_PATTERN 的复合启发式（不再仅依赖 title 前缀）。
    PHIType.PATIENT_NAME: _PATIENT_NAME_PATTERN,
    # —— 以下为新增的 7 类正则模式（FAX 走后处理，不在此列）——
    # 账号
    PHIType.ACCOUNT_NUMBER: (
        r"\b(?i:account|acct)\s*(?i:#|no\.?|number|id)?\s*:?\s*[A-Z0-9]{6,20}\b"
        r"|(?:账号|账户)\s*号?\s*:?\s*[A-Z0-9]{6,20}\b"
    ),
    # 执照 / 许可证号
    PHIType.LICENSE_NUMBER: (
        r"\b(?i:license|licence|lic)\s*(?i:#|no\.?|number|id)?\s*:?\s*[A-Z0-9]{5,20}\b"
        r"|(?:执照|许可证)\s*号?\s*:?\s*[A-Z0-9]{5,20}\b"
    ),
    # 车辆标识：VIN + 中国车牌
    PHIType.VEHICLE_ID: (
        r"\b(?i:vin)\s*:?\s*[A-Z0-9]{5,17}\b"
        r"|[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领]"
        r"[A-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]"
    ),
    # 设备 / 序列号
    PHIType.DEVICE_ID: (
        r"\b(?i:device|serial)\s*(?i:#|no\.?|number|id|sn|s/n)?\s*:?\s*[A-Z0-9]{6,20}\b"
        r"|(?:设备|序列号)\s*号?\s*:?\s*[A-Z0-9]{6,20}\b"
    ),
    # 网址（http(s):// 或 www.）
    PHIType.URL: r"""https?://[^\s<>"']+|www\.[^\s<>"']+""",
    # 生物标识
    PHIType.BIO_METRIC: (
        r"\b(?i:fingerprint|retina|voiceprint|faceprint)\b"
        r"|指纹|视网膜|声纹|面部识别"
    ),
    # 面部照片引用（图片文件名）
    PHIType.FULL_FACE: (
        r"\b(?i:photo|portrait)\s*:?\s*[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|gif|bmp)\b"
        r"|(?:照片|头像)\s*:?\s*[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|gif|bmp)\b"
    ),
    # 中国大陆18位身份证号：以1-9开头，17位数字 + 1位数字或X/x
    PHIType.ID_CARD: r"\b[1-9]\d{16}[\dXx]\b",
}


def _build_patterns() -> list[tuple[PHIType, re.Pattern[str]]]:
    """Compile the merged DLP + medical pattern set, DOB before DATE."""
    # Order: DOB first so its label-anchored match wins over the generic
    # DATE pattern during de-duplication (DOB spans are contained in or
    # overlap DATE spans).
    ordered: list[tuple[PHIType, str]] = []
    ordered.append((PHIType.DOB, _MEDICAL_PATTERNS[PHIType.DOB]))
    ordered.extend((t, p) for t, p in _DLP_REUSE.items())
    ordered.extend((t, p) for t, p in _MEDICAL_PATTERNS.items() if t is not PHIType.DOB)
    return [(t, re.compile(p)) for t, p in ordered]


class PHIDetector:
    """Detect and de-identify PHI in free text.

    Example:
        >>> d = PHIDetector()
        >>> d.deidentify("Patient John Doe MRN 12345678 called 555-123-4567")
        'Patient [REDACTED] [REDACTED] called [REDACTED]'
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # ``config`` is reserved for future per-field enable/disable and
        # custom pattern injection. It is accepted now so callers can pin
        # the signature without a later breaking change. ``pseudonym_salt``
        # overrides the default SHA-256 salt used by :meth:`pseudonymize`.
        self._config: dict[str, Any] = dict(config or {})
        self._pseudonym_salt: str = str(self._config.get("pseudonym_salt", _DEFAULT_PSEUDONYM_SALT))
        self._patterns: list[tuple[PHIType, re.Pattern[str]]] = _build_patterns()
        # NER model: constructor key wins over env; empty disables.
        self._spacy_model: str = str(
            self._config.get(
                "spacy_model",
                os.environ.get("DOCTORAGENT_SECURITY__DEID_SPACY_MODEL", ""),
            )
        ).strip()

    def _detect_names_ner(self, text: str) -> list[tuple[int, int, PHIType, str]]:
        """ spaCy PERSON-entity pass (no-op when the layer is unavailable)."""
        if not text:
            return []
        nlp = _get_ner_pipeline(self._spacy_model)
        if nlp is None:
            return []
        try:
            doc = nlp(text)
        except Exception:  # noqa: BLE001 — NER must never break detection
            logger.warning("NER pipeline raised during detection", exc_info=True)
            return []
        spans: list[tuple[int, int, PHIType, str]] = []
        for ent in doc.ents:
            label = getattr(ent, "label_", "")
            if label in ("PERSON", "PER"):
                spans.append(
                    (
                        ent.start_char,
                        ent.end_char,
                        PHIType.PATIENT_NAME,
                        ent.text,
                    )
                )
        return spans

    # ── detection ───────────────────────────────────────────────────────

    def detect_phi(self, text: str) -> list[dict[str, Any]]:
        """Return every PHI occurrence in *text*.

        Each entry is ``{type, value, start, end}`` where ``type`` is the
        :class:`PHIType` value (string) and ``start``/``end`` are character
        offsets into *text*. Overlapping matches are de-duplicated: when
        two matches overlap, the earlier-starting, longer one wins.
        """
        if not text:
            return []
        raw: list[tuple[int, int, PHIType, str]] = []
        for phi_type, pattern in self._patterns:
            for m in pattern.finditer(text):
                raw.append((m.start(), m.end(), phi_type, m.group(0)))
        # NER 增强：spaCy PERSON 实体并入 PATIENT_NAME 候选集，交由
        # 既有重叠去重与正则命中仲裁（更早/更长者胜）。
        raw.extend(self._detect_names_ner(text))
        # FAX 后处理：电话号码附近 ±20 字符内出现 fax/传真 关键词时，
        # 改判为 FAX（与单纯 PHONE 区分，便于差异化脱敏）。
        retagged: list[tuple[int, int, PHIType, str]] = []
        for start, end, phi_type, value in raw:
            if phi_type is PHIType.PHONE:
                ctx_start = max(0, start - 20)
                ctx_end = min(len(text), end + 20)
                if _FAX_KEYWORD_RE.search(text[ctx_start:ctx_end]):
                    phi_type = PHIType.FAX
            retagged.append((start, end, phi_type, value))
        return [
            {"type": str(phi_type), "value": value, "start": start, "end": end}
            for start, end, phi_type, value in self._dedupe(retagged)
        ]

    @staticmethod
    def _dedupe(
        matches: list[tuple[int, int, PHIType, str]],
    ) -> list[tuple[int, int, PHIType, str]]:
        """Drop matches fully contained in an earlier, longer match."""
        ordered = sorted(matches, key=lambda m: (m[0], -(m[1] - m[0])))
        kept: list[tuple[int, int, PHIType, str]] = []
        for m in ordered:
            contained = any(
                k[0] <= m[0] and m[1] <= k[1] and (k[1] - k[0]) >= (m[1] - m[0]) for k in kept
            )
            if not contained:
                kept.append(m)
        kept.sort(key=lambda m: m[0])
        return kept

    # ── de-identification ───────────────────────────────────────────────

    def deidentify(self, text: str, strategy: str = "redact") -> str:
        """De-identify *text* using *strategy*.

        ``strategy`` is one of ``redact``, ``pseudonymize`` or ``mask``.
        Unknown strategies raise :class:`ValueError`. Text with no PHI is
        returned unchanged.
        """
        if strategy not in ("redact", "pseudonymize", "mask"):
            raise ValueError(f"Unknown de-identification strategy: {strategy!r}")
        matches = self.detect_phi(text)
        if not matches:
            return text
        if strategy == "redact":
            return self._apply_replacements(text, matches, lambda m: "[REDACTED]")
        if strategy == "mask":
            return self._apply_replacements(
                text, matches, lambda m: self._mask(m["type"], m["value"])
            )
        # pseudonymize
        redacted, _mapping = self.pseudonymize(text)
        return redacted

    def pseudonymize(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace each unique PHI value with a stable type-tagged placeholder.

        Returns ``(deidentified_text, mapping)`` where *mapping* is
        ``{placeholder: original_value}``. Use :meth:`restore` to reverse
        the substitution. The same value always maps to the same
        placeholder within a single call, so repeated occurrences stay
        consistent.
        """
        matches = self.detect_phi(text)
        if not matches:
            return text, {}
        value_to_placeholder: dict[tuple[str, str], str] = {}
        mapping: dict[str, str] = {}
        spans: list[tuple[int, int, str]] = []
        for m in matches:
            phi_type = m["type"]
            value = m["value"]
            key = (phi_type, value)
            placeholder = value_to_placeholder.get(key)
            if placeholder is None:
                tag = phi_type.split("_")[0]
                # SHA-256(salt + value) 生成稳定且不可逆的占位符摘要
                # （MD5 已弃用，改用 SHA-256 + 盐以降低碰撞与预画像攻击）。
                digest = hashlib.sha256((self._pseudonym_salt + value).encode("utf-8")).hexdigest()[
                    :8
                ]
                placeholder = f"[{tag}_{digest}]"
                value_to_placeholder[key] = placeholder
                mapping[placeholder] = value
            spans.append((m["start"], m["end"], placeholder))
        return self._splice(text, spans), mapping

    def restore(self, text: str, mapping: dict[str, str]) -> str:
        """Replace each placeholder in *mapping* with its original value."""
        if not mapping:
            return text
        result = text
        for placeholder, original in mapping.items():
            result = result.replace(placeholder, original)
        return result

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _mask(phi_type: str, value: str) -> str:
        """Return a partially-masked representation of *value*."""

        def _keep_last4(val: str) -> str:
            digits = re.sub(r"\D", "", val)
            if len(digits) < 4:
                return "*" * len(val)
            return "*" * (len(digits) - 4) + digits[-4:]

        if phi_type == str(PHIType.PHONE):
            return _keep_last4(value)
        if phi_type == str(PHIType.EMAIL):
            if "@" in value:
                local, domain = value.split("@", 1)
                masked_local = (local[0] + "*" * max(1, len(local) - 1)) if local else "*"
                return f"{masked_local}@{domain}"
            return "*" * len(value)
        if phi_type == str(PHIType.SSN):
            digits = re.sub(r"\D", "", value)
            return f"***-**-{digits[-4:]}" if len(digits) >= 4 else "*" * len(value)
        if phi_type in (
            str(PHIType.MRN),
            str(PHIType.MEDICAL_RECORD),
            str(PHIType.FAX),
            str(PHIType.ACCOUNT_NUMBER),
            str(PHIType.LICENSE_NUMBER),
            str(PHIType.DEVICE_ID),
            str(PHIType.VEHICLE_ID),
            str(PHIType.ID_CARD),
        ):
            # 数字 / 字母数字标识符：保留末 4 位作为提示。
            return _keep_last4(value)
        if phi_type in (str(PHIType.DATE), str(PHIType.DOB)):
            return "[DATE]"
        if phi_type == str(PHIType.ADDRESS):
            return "[ADDRESS]"
        if phi_type == str(PHIType.IP_ADDRESS):
            parts = value.split(".")
            if len(parts) == 4:
                return f"{parts[0]}.{parts[1]}.x.x"
            return "*.*.*.*"
        if phi_type == str(PHIType.URL):
            return "[URL]"
        if phi_type == str(PHIType.PATIENT_NAME):
            tokens = value.split()
            if len(tokens) <= 1:
                # 单 token 姓名（含中文姓名）：整体打码为等长星号。
                return "*" * len(value)
            # 保留称谓前缀，姓名 token 仅保留首字母。
            title = tokens[0]
            names = tokens[1:]
            masked = " ".join(t[0] + "*" * (len(t) - 1) for t in names)
            return f"{title} {masked}"
        # BIO_METRIC / FULL_FACE 等无结构标识：整体打码。
        return "[REDACTED]"

    @staticmethod
    def _apply_replacements(
        text: str,
        matches: list[dict[str, Any]],
        transform: Any,
    ) -> str:
        """Apply *transform(match)* to each match, splicing from end to start."""
        spans = [(m["start"], m["end"], transform(m)) for m in matches]
        return PHIDetector._splice(text, spans)

    @staticmethod
    def _splice(text: str, spans: list[tuple[int, int, str]]) -> str:
        """Splice replacements into *text* from rightmost to leftmost."""
        result = text
        for start, end, replacement in sorted(spans, key=lambda s: s[0], reverse=True):
            result = result[:start] + replacement + result[end:]
        return result
