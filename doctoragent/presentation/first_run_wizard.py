"""First-run setup wizard for DoctorAgent."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from PyQt6.QtWidgets import (
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
        QWizard,
        QWizardPage,
    )
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "PyQt6 is required for the DoctorAgent GUI. Install the GUI extra: pip install 'doctoragent[gui]'"
    ) from exc

from doctoragent.config import AegisConfig
from doctoragent.presentation.utils import validate_http_url

_DIGIT = re.compile(r"\d")
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_SPECIAL = re.compile(r"[!@#$%^&*()\-_=+\[\]{};:'\",.<>/?\\|`~]")


def password_strength(password: str) -> tuple[str, str]:
    """Return (label, colour) for *password* strength."""
    if not password:
        return ("", "")
    types = sum(bool(p.search(password)) for p in (_DIGIT, _UPPER, _LOWER, _SPECIAL))
    length = len(password)
    if length < 8:
        return ("Weak", "red")
    if length < 12 or types < 3:
        return ("Medium", "orange")
    if length < 16 or types < 4:
        return ("Strong", "green")
    return ("Very Strong", "darkgreen")


class WelcomePage(QWizardPage):
    """Welcome / introduction page."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("欢迎使用 DoctorAgent")
        self.setSubTitle("临床AI智能体平台 — 用药安全审查 · 危急值预警 · 病历文书 · 合规审计")

        layout = QVBoxLayout(self)

        intro = QLabel(
            "DoctorAgent 是合规优先、本地部署的临床AI智能体平台。\n\n"
            "核心能力:\n"
            "  • 用药安全审查（药物相互作用 / 过敏交叉反应 / 重复用药）\n"
            "  • 危急值预警（生命体征与检验指标，确定性规则优先于 LLM）\n"
            "  • 病历文书生成（SOAP 格式 / ICD-10 编码辅助）\n"
            "  • 合规审计（HMAC 审计链 / PHI 脱敏 / FHIR R4 / CDS Hooks）\n"
            "  • 本地加密 Vault：可用于医学文献与指南的本地化管理\n\n"
            "本向导将帮助您完成 DoctorAgent 的首次配置。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addStretch()


class PathsPage(QWizardPage):
    """Inbox / Vault directory selection."""

    def __init__(self, config: AegisConfig) -> None:
        super().__init__()
        self.setTitle("路径设置")
        self.setSubTitle("选择 DoctorAgent 的文件存储位置")

        form = QFormLayout(self)

        self._inbox_edit = QLineEdit(str(config.paths.inbox))
        self._vault_edit = QLineEdit(str(config.paths.vault))

        form.addRow("Inbox 目录:", self._browse_row(self._inbox_edit, "选择 Inbox 目录"))
        form.addRow("Vault 目录:", self._browse_row(self._vault_edit, "选择 Vault 目录"))

        tip = QLabel(
            "<small>Inbox: 放入此目录的文件将被自动处理。\n"
            "Vault: 处理完成后的文件将存储在此处。</small>"
        )
        tip.setWordWrap(True)
        form.addRow(tip)

        self.registerField("inbox_path*", self._inbox_edit)
        self.registerField("vault_path*", self._vault_edit)

    @staticmethod
    def _browse_row(edit: QLineEdit, caption: str) -> QHBoxLayout:
        """Return a horizontal layout pairing *edit* with a browse button."""
        row = QHBoxLayout()
        row.addWidget(edit)
        btn = QPushButton("浏览...")
        btn.clicked.connect(lambda: PathsPage._browse_dir(edit, caption))
        row.addWidget(btn)
        return row

    @staticmethod
    def _browse_dir(edit: QLineEdit, caption: str) -> None:
        """Open a directory chooser and update *edit*."""
        current = Path(edit.text())
        if not current.exists():
            current = Path.home()
        chosen = QFileDialog.getExistingDirectory(None, caption, str(current))
        if chosen:
            edit.setText(chosen)


class ModelPage(QWizardPage):
    """Local model connection configuration."""

    def __init__(self, config: AegisConfig) -> None:
        super().__init__()
        self.setTitle("模型连接")
        self.setSubTitle("配置本地 AI 模型端点")

        form = QFormLayout(self)

        self._url_edit = QLineEdit(config.model.base_url)
        self._url_edit.setPlaceholderText("http://127.0.0.1:11434/v1")

        self._name_edit = QLineEdit(config.model.model_name)
        self._name_edit.setPlaceholderText("qwen2.5:7b")

        form.addRow("模型 URL:", self._url_edit)
        form.addRow("模型名称:", self._name_edit)

        self._test_btn = QPushButton("测试连接")
        self._test_label = QLabel("")
        # Reuse a single executor for all test-connection attempts instead of
        # creating a new one (and shutting it down) on every button click.
        self._test_executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
        self._test_in_progress = False
        test_row = QHBoxLayout()
        test_row.addWidget(self._test_btn)
        test_row.addWidget(self._test_label)
        test_row.addStretch()
        form.addRow(test_row)

        self._test_btn.clicked.connect(self._on_test)

        tip = QLabel(
            "<small>对于 Ollama，默认 URL 为 http://127.0.0.1:11434/v1。\n"
            "您可稍后在设置中修改此配置。</small>"
        )
        tip.setWordWrap(True)
        form.addRow(tip)

        self.registerField("model_url*", self._url_edit)
        self.registerField("model_name*", self._name_edit)

    def _on_test(self) -> None:
        """Test the model connection on a background thread.

        Avoids blocking the UI thread with the httpx sync call.
        """
        if self._test_in_progress:
            return

        import httpx
        from PyQt6.QtCore import QTimer

        url = self._url_edit.text().strip()
        self._test_in_progress = True
        self._test_btn.setEnabled(False)
        self._test_label.setText("正在测试...")

        def run_test() -> None:
            """Run the httpx request in a sub-thread.

            Switches back to the UI thread via QTimer to update the
            widgets once it finishes.
            """
            try:
                with httpx.Client(timeout=5) as client:
                    resp = client.get(f"{url.rstrip('/')}/models")
                    if resp.is_success:
                        msg = "连接成功"
                    else:
                        msg = f"失败 ({resp.status_code})"
            except httpx.ConnectError:
                msg = "无法连接 — 请检查 URL"
            except httpx.TimeoutException:
                msg = "连接超时 — 服务未响应"
            except Exception as exc:
                msg = f"测试失败: {exc}"
            # QTimer.singleShot is thread-safe; the callback runs on the UI thread's event loop.
            QTimer.singleShot(0, lambda: self._apply_test_result(msg))

        self._test_executor.submit(run_test)

    def _apply_test_result(self, message: str) -> None:
        """Update the test-result widgets on the UI thread."""
        self._test_label.setText(message)
        self._test_btn.setEnabled(True)
        self._test_in_progress = False


class SecurityPage(QWizardPage):
    """Master password / security configuration."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("安全设置")
        self.setSubTitle("创建主密钥密码以保护临床数据与本地加密存储")

        form = QFormLayout(self)

        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("输入主密钥密码")

        self._confirm_edit = QLineEdit()
        self._confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._confirm_edit.setPlaceholderText("确认主密钥密码")

        self._strength_label = QLabel("")

        form.addRow("主密钥密码:", self._password_edit)
        form.addRow("确认密码:", self._confirm_edit)
        form.addRow("密码强度:", self._strength_label)

        self._password_edit.textChanged.connect(self._update_strength)

        warning = QLabel("<b>重要提示:</b> 主密钥密码无法恢复。\n请务必妥善备份！")
        warning.setWordWrap(True)
        form.addRow(warning)

        self.registerField("master_password*", self._password_edit)
        self.registerField("confirm_password*", self._confirm_edit)

    def _update_strength(self, text: str) -> None:
        """Update the password strength indicator."""
        label, colour = password_strength(text)
        if label:
            self._strength_label.setText(
                f'<span style="color:{colour};font-weight:bold">{label}</span>'
            )
        else:
            self._strength_label.setText("")

    def validatePage(self) -> bool:  # noqa: N802
        """Validate that passwords match and meet minimum strength."""
        pwd = str(self.field("master_password"))
        confirm = str(self.field("confirm_password"))
        if not pwd:
            QMessageBox.warning(self, "密码为空", "主密钥密码不能为空。")
            return False
        if pwd != confirm:
            QMessageBox.warning(self, "密码不匹配", "两次输入的密码不一致。")
            return False
        if len(pwd) < 8:
            QMessageBox.warning(self, "密码太弱", "密码至少需要 8 个字符。")
            return False
        return True


class FinishPage(QWizardPage):
    """Configuration summary and finish."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("设置完成")
        self.setSubTitle("确认您的配置")

        layout = QVBoxLayout(self)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)
        layout.addStretch()

    def initializePage(self) -> None:  # noqa: N802
        """Build the summary from collected wizard fields."""
        w = self.wizard()
        if w is None:
            self._summary.setText("Error: wizard context lost.")
            return
        inbox = w.field("inbox_path")
        vault = w.field("vault_path")
        url = w.field("model_url")
        model = w.field("model_name")
        has_pwd = bool(w.field("master_password"))

        lines = [
            "<b>配置摘要:</b>",
            "",
            f"  Inbox 目录: {inbox}",
            f"  Vault 目录: {vault}",
            f"  模型 URL: {url}",
            f"  模型名称: {model}",
            f"  主密钥密码: {'已设置' if has_pwd else '未设置'}",
            "",
            "点击“完成”保存配置并启动 DoctorAgent。",
        ]
        self._summary.setText("<br>".join(lines))


class FirstRunWizard(QWizard):
    """First-run setup wizard that collects and saves DoctorAgent configuration."""

    def __init__(self, config: AegisConfig, parent: QWizard | None = None) -> None:
        super().__init__(parent)
        self._config = config

        self.setWindowTitle("DoctorAgent 设置向导")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        self.addPage(WelcomePage())
        self.addPage(PathsPage(config))
        self.addPage(ModelPage(config))
        self.addPage(SecurityPage())
        self.addPage(FinishPage())

    def accept(self) -> None:
        """Apply wizard fields to the configuration and persist to disk."""
        inbox = Path(str(self.field("inbox_path")))
        vault = Path(str(self.field("vault_path")))
        model_url = str(self.field("model_url"))
        model_name = str(self.field("model_name"))
        password = str(self.field("master_password"))

        # Validate that model_url is a usable http(s) URL with a host before
        # persisting a configuration that the model client cannot use.
        if not validate_http_url(model_url):
            QMessageBox.warning(
                self,
                "URL 无效",
                "模型 URL 必须是有效的 http:// 或 https:// 地址。",
            )
            return

        # Try to create the inbox / vault directories now so we surface a
        # clear error instead of letting DoctorAgent fail on first use.
        for path, label in ((inbox, "Inbox"), (vault, "Vault")):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    "路径不可创建",
                    f"{label} 路径 '{path}' 无法创建: {exc}",
                )
                return

        self._config.paths.inbox = inbox
        self._config.paths.vault = vault
        self._config.model.base_url = model_url
        self._config.model.model_name = model_name
        if password:
            self._config.security.master_key_password = password
            self._config.security.master_key_provider = "FilePassword"

        self._config.save_to_file()
        super().accept()
