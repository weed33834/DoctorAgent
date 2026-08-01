"""Shamir's Secret Sharing (k-of-n threshold scheme) for master key protection.

Splits a secret into n shares, any k of which can reconstruct the original.
This provides:
- Elimination of single-point-of-failure for master keys
- Multi-party authorization for sensitive operations
- Information-theoretic security (shares reveal no information individually)

Implementation uses polynomial interpolation over GF(256) for byte-level
secret sharing, which is the standard approach used by ssss and other tools.

The secret is processed one byte at a time.  For each byte a random
polynomial of degree ``k - 1`` is constructed whose constant term is the
secret byte; share ``i`` then contains ``f(i)`` for every byte position.
Reconstruction uses Lagrange interpolation evaluated at ``x = 0``.

GF(256) arithmetic uses the standard irreducible polynomial
``0x11b`` (``x^8 + x^4 + x^3 + x + 1``), so addition is XOR and every
non-zero element has a multiplicative inverse (computed here via Fermat's
little theorem: ``a^254 == a^-1``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Irreducible polynomial used for GF(256) reduction.  Only the low 8 bits
# (0x1b) are needed after masking the carry out of the high byte.
_IRREDUCIBLE_POLY = 0x11B
_REDUCE_MASK = 0x1B  # low byte of 0x11b

# Share metadata is round-tripped through the hex helpers using this
# separator.  Chosen because it is illegal inside a hex string.
_HEX_META_SEP = ":"
_HEX_FIELD_SEP = "-"


@dataclass
class Share:
    """A single Shamir secret share.

    Attributes:
        index: The x-coordinate (evaluation point) of this share, in the
            range ``[1, total]``.  ``0`` is reserved for the secret itself.
        data: The share payload — ``f(index)`` evaluated byte-by-byte over
            the per-byte polynomials.  Length equals the secret length.
        threshold: ``k`` — the minimum number of shares required to
            reconstruct the secret.
        total: ``n`` — the total number of shares that were generated.
    """

    index: int
    data: bytes
    threshold: int
    total: int

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")
        if self.total < self.threshold:
            raise ValueError("total must be >= threshold")
        if not (1 <= self.index <= self.total):
            raise ValueError(f"share index {self.index} must be within [1, {self.total}]")


# ── GF(256) arithmetic ────────────────────────────────────────────────────────


def _gf256_multiply(a: int, b: int) -> int:
    """Multiply two elements of GF(256) modulo ``0x11b``.

    Uses the Russian-peasant (shift-and-XOR) algorithm.  Whenever a left
    shift would carry out of the 8th bit the result is reduced by XOR with
    the low byte of the irreducible polynomial.
    """
    a &= 0xFF
    b &= 0xFF
    result = 0
    while b:
        if b & 1:
            result ^= a
        b >>= 1
        carry = a & 0x80
        a = (a << 1) & 0xFF
        if carry:
            a ^= _REDUCE_MASK
    return result & 0xFF


def _gf256_pow(a: int, n: int) -> int:
    """Compute ``a ** n`` in GF(256) using fast exponentiation.

    ``n`` is treated as an ordinary (non-negative) integer exponent; the
    base and result live in GF(256).
    """
    if n < 0:
        raise ValueError("exponent must be non-negative")
    result = 1  # the multiplicative identity
    base = a & 0xFF
    while n > 0:
        if n & 1:
            result = _gf256_multiply(result, base)
        n >>= 1
        if n:  # avoid one needless squaring on the final iteration
            base = _gf256_multiply(base, base)
    return result


def _gf256_inverse(a: int) -> int:
    """Return the multiplicative inverse of *a* in GF(256).

    By Fermat's little theorem ``a ^ 255 == 1`` for every non-zero element,
    so ``a ^ 254 == a ^ -1``.  The inverse of ``0`` is undefined and raises
    :class:`ZeroDivisionError`.
    """
    a &= 0xFF
    if a == 0:
        raise ZeroDivisionError("0 has no multiplicative inverse in GF(256)")
    return _gf256_pow(a, 254)


def _eval_poly(coeffs: list[int], x: int) -> int:
    """Evaluate a polynomial at *x* over GF(256) using Horner's method.

    ``coeffs[0]`` is the constant term, ``coeffs[-1]`` the highest-degree
    coefficient.
    """
    result = 0
    for coeff in reversed(coeffs):
        result = _gf256_multiply(result, x) ^ (coeff & 0xFF)
    return result


def _lagrange_interpolation(points: list[tuple[int, int]], x: int) -> int:
    """Evaluate the Lagrange interpolating polynomial at *x* over GF(256).

    *points* is a list of ``(x_i, y_i)`` pairs.  In GF(256) subtraction is
    identical to addition (XOR), so ``(x - x_j)`` becomes ``x ^ x_j``.
    """
    total = 0
    count = len(points)
    for i in range(count):
        xi, yi = points[i]
        num = 1
        den = 1
        for j in range(count):
            if i == j:
                continue
            xj = points[j][0]
            num = _gf256_multiply(num, x ^ xj)
            den = _gf256_multiply(den, xi ^ xj)
        # term = yi * num / den  ==  yi * num * den^-1
        term = _gf256_multiply(yi, _gf256_multiply(num, _gf256_inverse(den)))
        total ^= term
    return total


class ShamirSecretSharing:
    """Split and reconstruct secrets using Shamir's k-of-n scheme.

    The class is stateless aside from logging — every operation is a pure
    function of its inputs, which makes it safe to share a single instance
    across threads.

    Example:
        >>> sss = ShamirSecretSharing()
        >>> shares = sss.split(b"top secret", threshold=3, total=5)
        >>> sss.reconstruct(shares[:3]) == b"top secret"
        True
    """

    # ── splitting ────────────────────────────────────────────────────────

    def split(self, secret: bytes, threshold: int, total: int) -> list[Share]:
        """Split *secret* into *total* shares, *threshold* of which reconstruct.

        Args:
            secret: The bytes to protect.
            threshold: ``k`` — minimum shares required to reconstruct.
                Must satisfy ``1 <= threshold <= total``.  Values below 2
                provide no secrecy and are accepted only for completeness.
            total: ``n`` — the number of shares to generate.

        Returns:
            A list of :class:`Share` objects of length *total*.

        Raises:
            ValueError: If the parameters are inconsistent.
        """
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if total < 1:
            raise ValueError("total must be >= 1")
        if threshold > total:
            raise ValueError("threshold cannot exceed total")

        secret_len = len(secret)

        # Build the coefficient matrix: ``threshold`` rows, ``secret_len``
        # columns.  Row 0 is the secret itself; rows 1..threshold-1 are
        # uniformly random — this is what makes individual shares
        # information-theoretically useless.
        coeffs: list[list[int]] = [list(secret)]
        if threshold > 1:
            random_block = os.urandom((threshold - 1) * secret_len)
            for r in range(threshold - 1):
                start = r * secret_len
                coeffs.append(list(random_block[start : start + secret_len]))

        shares: list[Share] = []
        for index in range(1, total + 1):
            payload = bytearray(secret_len)
            for pos in range(secret_len):
                column = [coeffs[row][pos] for row in range(threshold)]
                payload[pos] = _eval_poly(column, index)
            shares.append(
                Share(
                    index=index,
                    data=bytes(payload),
                    threshold=threshold,
                    total=total,
                )
            )

        logger.debug(
            "Split %d-byte secret into %d shares (threshold=%d)", secret_len, total, threshold
        )
        return shares

    # ── reconstruction ───────────────────────────────────────────────────

    def reconstruct(self, shares: list[Share]) -> bytes:
        """Reconstruct the original secret from *k* or more shares.

        Args:
            shares: At least ``threshold`` :class:`Share` objects.  Extra
                shares beyond the threshold are harmless — they simply
                provide redundant interpolation points.

        Returns:
            The reconstructed secret bytes.

        Raises:
            ValueError: If too few shares are supplied or the shares are
                inconsistent (mismatched threshold/total/length or
                duplicate indices).
        """
        if not shares:
            raise ValueError("cannot reconstruct from an empty share list")

        threshold = shares[0].threshold
        total = shares[0].total
        if len(shares) < threshold:
            raise ValueError(f"need at least {threshold} shares to reconstruct, got {len(shares)}")

        data_len = len(shares[0].data)
        seen_indices: set[int] = set()
        for share in shares:
            if share.threshold != threshold or share.total != total:
                raise ValueError("shares have inconsistent threshold/total metadata")
            if len(share.data) != data_len:
                raise ValueError("shares have inconsistent data length")
            if share.index in seen_indices:
                raise ValueError(f"duplicate share index {share.index}")
            seen_indices.add(share.index)

        secret = bytearray(data_len)
        for pos in range(data_len):
            points = [(share.index, share.data[pos]) for share in shares]
            secret[pos] = _lagrange_interpolation(points, 0)

        logger.debug(
            "Reconstructed %d-byte secret from %d shares (threshold=%d)",
            data_len,
            len(shares),
            threshold,
        )
        return bytes(secret)

    # ── hex convenience helpers ──────────────────────────────────────────

    def split_hex(self, secret_hex: str, threshold: int, total: int) -> list[str]:
        """Split a hex-encoded secret, returning hex-encoded shares.

        Each returned string encodes the share metadata and payload as
        ``"<index>-<threshold>-<total>:<data-hex>"`` so it is a single
        self-contained token that can be distributed independently.
        """
        try:
            secret = bytes.fromhex(secret_hex)
        except ValueError as exc:
            raise ValueError(f"invalid hex secret: {exc}") from exc

        shares = self.split(secret, threshold, total)
        return [
            f"{share.index}{_HEX_FIELD_SEP}{share.threshold}{_HEX_FIELD_SEP}{share.total}"
            f"{_HEX_META_SEP}{share.data.hex()}"
            for share in shares
        ]

    def reconstruct_hex(self, shares: list[str]) -> str:
        """Reconstruct a hex secret from hex-encoded share tokens.

        The tokens must be in the format produced by :meth:`split_hex`.
        """
        parsed: list[Share] = []
        for token in shares:
            try:
                meta, data_hex = token.split(_HEX_META_SEP, 1)
                index_str, threshold_str, total_str = meta.split(_HEX_FIELD_SEP)
                parsed.append(
                    Share(
                        index=int(index_str),
                        data=bytes.fromhex(data_hex),
                        threshold=int(threshold_str),
                        total=int(total_str),
                    )
                )
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"malformed share token: {exc}") from exc

        return self.reconstruct(parsed).hex()
