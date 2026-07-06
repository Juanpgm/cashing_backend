"""Prompts y constantes para el filtro de ruido de evidencias (trabajo vs. ruido)."""

from __future__ import annotations

import re

# ── Scoring system — non-personal email detection ────────────────────────────
#
# Score accumulation rules (spec §5):
#   +5  definitive header present   → filter immediately
#   +4  known platform/service domain
#   +3  automatic From prefix
#   +2  high-confidence subject pattern
#   +1  broad subject noise pattern
#
# Threshold ≥ 3 → non-personal (heuristic layer drops it, never reaches LLM).

# Headers whose mere presence guarantees a bulk/automated sender.
_DEFINITIVE_HEADERS: frozenset[str] = frozenset({
    "list-unsubscribe",
    "list-unsubscribe-post",
    "list-id",
    "list-post",
    "list-owner",
    "x-campaign",
    "x-campaign-id",
    "x-mc-campaign",
    "x-feedback-id",
    # Platform-injected
    "x-github-reason",
    "x-github-sender",
    "x-github-target",
    "x-notifications",
    "x-linkedin-class",
    "x-linkedin-id",
    "x-atlassian-token",
    "x-jira-fingerprint",
})

_ESP_XMAILER_NAMES: tuple[str, ...] = (
    "mailchimp",
    "sendgrid",
    "hubspot",
    "klaviyo",
    "brevo",
    "mailgun",
    "postmark",
    "campaign monitor",
)

_PRECEDENCE_BULK_VALUES: frozenset[str] = frozenset({"bulk", "list", "junk"})

# Well-known platform domains — presence alone is +4.
_PLATFORM_DOMAINS: frozenset[str] = frozenset({
    # Dev / CI / PM
    "github.com", "gitlab.com", "bitbucket.org",
    "jira.atlassian.com", "trello.com", "circleci.com",
    "travis-ci.org", "vercel.com", "netlify.com",
    "heroku.com", "render.com",
    # Social
    "linkedin.com", "twitter.com", "x.com",
    "facebookmail.com", "instagram.com", "tiktok.com",
    "youtube.com", "pinterest.com", "reddit.com",
    "discord.com", "slack.com",
    # Services / SaaS
    "paypal.com", "stripe.com", "shopify.com",
    "amazon.com", "apple.com", "microsoft.com",
    "google.com", "notion.so", "figma.com",
    "zoom.us", "dropbox.com", "samsung.com",
    # Google notification subdomain
    "notifications.google.com",
})

# ESP sending domains (wildcard subdomain match) — +5.
_ESP_DOMAIN_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\.mailchimp\.com$",
        r"\.sendgrid\.net$",
        r"\.klaviyo\.com$",
        r"\.hubspot\.com$",
        r"\.brevo\.com$",
        r"\.mailgun\.org$",
        r"\.postmarkapp\.com$",
        r"\.campaignmonitor\.com$",
        r"\.constantcontact\.com$",
        r"\.aweber\.com$",
    )
)

# Normalized auto-prefixes (hyphens/dots stripped, lowercase).
# "no-reply" → "noreply", "no.reply" → "noreply", etc.
_AUTO_PREFIXES_NORMALIZED: frozenset[str] = frozenset({
    # English
    "noreply", "donotreply", "notifications", "notification",
    "newsletter", "alerts", "alert", "mailer", "postmaster",
    "bounce", "support", "help", "team", "admin", "info",
    "marketing", "promo", "hello", "hi", "news", "updates", "update",
    "offers", "offer", "reply", "billing", "receipts", "receipt",
    "orders", "order", "confirm", "verify", "security",
    "automated", "robot", "bot",
    # Spanish equivalents
    "facturacion", "factura", "cobros", "cobro", "pagos", "pago",
    "soporte", "ayuda", "equipo", "noticias", "actualizaciones",
    "actualizacion", "ofertas", "oferta", "confirmacion", "confirma",
    "verificacion", "verifica", "seguridad", "automatizado",
    "admisiones", "admision",
})

# High-confidence subject patterns → +2.
_SUBJECT_HIGH_CONFIDENCE: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Transactional
        r"your receipt",
        r"order\s*#",
        r"invoice\s*#",
        r"tu recibo",
        r"confirmaci[oó]n de pago",
        r"password reset",
        r"verify your email",
        r"confirma tu cuenta",
        r"\b2fa\b",
        r"\botp\b",
        r"login attempt",
        r"security alert",
        r"alerta de seguridad",
        # Promotional
        r"\d+\s*%\s*off",
        r"limited time",
        r"exclusive offer",
        r"don.t miss out",
        r"sale ends",
        r"free trial",
        r"upgrade now",
        r"oferta especial",
        # Platforms
        r"commented on",
        r"mentioned you",
        r"pushed to",
        r"pull request",
        r"new follower",
        r"te invit[oó] a",
        r"viewed your profile",
        r"\bdigest\b",
        r"weekly summary",
        r"resumen semanal",
        r"new video from",
        r"is live now",
        # Support / Ticketing
        r"ticket\s*#",
        r"case\s*#",
        r"your request",
        r"we received your",
        r"tu caso",
        r"hemos recibido",
        r"\[auto\]",
        r"\[automated\]",
        r"auto.reply",
        r"out of office",
        r"respuesta autom[aá]tica",
    )
)

# ── Whitelist — personal and institutional domains never filtered ─────────────

_PERSONAL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "hotmail.es",
    "yahoo.com",
    "yahoo.es",
    "icloud.com",
    "live.com",
    "live.com.co",
    "protonmail.com",
    "me.com",
    "googlemail.com",
})

# Domain suffixes — any domain ending with one of these is institutional.
_INSTITUTIONAL_SUFFIXES: tuple[str, ...] = (
    ".gov.co",
    ".gov",
    ".edu.co",
    ".edu",
    ".mil.co",
    ".mil",
    ".org.co",
)


def _is_whitelisted(domain: str) -> bool:
    """True if domain is a personal email provider or institutional domain.

    Emails from these domains are never filtered regardless of other signals.
    """
    if domain in _PERSONAL_DOMAINS:
        return True
    for suffix in _INSTITUTIONAL_SUFFIXES:
        if domain.endswith(suffix):
            return True
    return False


# ── Email address parsing helpers ─────────────────────────────────────────────

_EMAIL_ADDR_RE = re.compile(r"([a-zA-Z0-9.+\-]+)@([a-zA-Z0-9.\-]+)")


def _extract_domain(sender: str) -> str:
    m = _EMAIL_ADDR_RE.search(sender)
    return m.group(2).lower() if m else ""


def _extract_user(sender: str) -> str:
    m = _EMAIL_ADDR_RE.search(sender)
    return m.group(1).lower() if m else ""


def _normalize_prefix(user: str) -> str:
    return re.sub(r"[.\-_]", "", user)


def _is_platform_domain(domain: str) -> bool:
    """True if domain is a known platform — exact match or subdomain of one.

    e.g. 'accountprotection.microsoft.com' matches 'microsoft.com'.
    """
    if domain in _PLATFORM_DOMAINS:
        return True
    for platform in _PLATFORM_DOMAINS:
        if domain.endswith("." + platform):
            return True
    return False


# ── Public scoring API ────────────────────────────────────────────────────────

def score_non_personal_email(
    sender: str,
    subject: str,
    labels: list[str],
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Accumulate non-personal signals for an email.

    Returns (score, main_reason). Threshold ≥ 3 = non-personal.

    Early-returns on definitive +5 signals to avoid redundant checks.
    """
    h = {k.lower(): v.lower() for k, v in (headers or {}).items()}

    # Whitelist — personal providers and institutional domains are never filtered.
    domain = _extract_domain(sender)
    if domain and _is_whitelisted(domain):
        return 0, ""

    # +5 — Gmail category labels (server-side ML classifier, highly accurate)
    for label in (labels or []):
        if label in NOISE_GMAIL_LABELS:
            return 5, f"gmail_label:{label}"

    # +5 — Definitive headers: presence alone = bulk/automated sender
    for hname in _DEFINITIVE_HEADERS:
        if hname in h:
            return 5, f"header:{hname}"

    # +5 — Precedence: bulk / list / junk
    prec = h.get("precedence", "").strip()
    if prec in _PRECEDENCE_BULK_VALUES:
        return 5, f"Precedence:{prec}"

    # +5 — X-Mailer identifying a known ESP
    xmailer = h.get("x-mailer", "")
    for esp in _ESP_XMAILER_NAMES:
        if esp in xmailer:
            return 5, f"X-Mailer:{esp}"

    score = 0
    reason = ""

    # +5 — ESP sending domain (wildcard match)
    if domain:
        for pattern in _ESP_DOMAIN_PATTERNS:
            if pattern.search(domain):
                return 5, f"esp_domain:{domain}"

    # +4 — Known platform domain (exact or subdomain match)
    if domain and _is_platform_domain(domain):
        score = 4
        reason = f"platform_domain:{domain}"
        return score, reason  # 4 ≥ 3 → already filter-worthy

    # +3 — Automatic From prefix
    user = _extract_user(sender)
    if user and _normalize_prefix(user) in _AUTO_PREFIXES_NORMALIZED:
        score += 3
        reason = f"auto_prefix:{user}"
        return score, reason

    # +3 — Legacy sender patterns (Colombian banks, telecos, payment platforms)
    if any(p.search(sender) for p in NOISE_SENDER_PATTERNS):
        score += 3
        reason = reason or "known_service_sender"
        return score, reason

    # +2 — High-confidence subject pattern
    for pattern in _SUBJECT_HIGH_CONFIDENCE:
        if pattern.search(subject):
            score += 2
            reason = reason or "subject_high_confidence"
            break

    if score >= 3:
        return score, reason

    # +1 — Broader subject noise (less specific, needs other signals to reach threshold)
    if not reason:
        for pattern in NOISE_SUBJECT_PATTERNS:
            if pattern.search(subject):
                score += 1
                reason = "subject_noise_pattern"
                break

    return score, reason


# ── Legacy boolean helpers (kept for backward compatibility) ──────────────────

NOISE_GMAIL_LABELS: frozenset[str] = frozenset({
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_FORUMS",
    "CATEGORY_UPDATES",
    "SPAM",
})

NOISE_SENDER_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # Generic automated senders
        r"no.?reply",
        r"noreply",
        r"donotreply",
        r"do.not.reply",
        r"newsletter",
        r"mailer",
        r"notifications?@",
        r"alerts?@",
        r"updates?@",
        r"marketing@",
        r"promo",
        r"offers?@",
        r"notifica",
        # Transactional / billing
        r"facturacion@",
        r"billing@",
        r"invoices?@",
        r"pagos?@",
        r"cobros?@",
        r"transacciones?@",
        r"extractos?@",
        r"servicios?@.*banco",
        r"alertas?@.*banco",
        # Colombian banks
        r"bancolombia",
        r"davivienda",
        r"nequi",
        r"bbva",
        r"scotiabank",
        r"itau",
        r"colpatria",
        r"occidente",
        r"bogota.*bank",
        r"banco.*bogota",
        r"banco.*popular",
        r"caja.*social",
        r"colmena",
        r"av\s*villas",
        r"coopcentral",
        r"bancamia",
        r"falabella",
        r"finandina",
        r"rapicredit",
        # Generic financial-institution / notification signals (local part or domain).
        # Catches any non-whitelisted bank domain (e.g. davibank.com) and the very
        # common Latam notification-sender pattern "<Entidad>Informa@…".
        r"bank",
        r"banco",
        r"informa@",
        # Payment / e-commerce platforms
        r"paypal",
        r"wompi",
        r"payu",
        r"mercadopago",
        r"mercadolibre",
        r"rappi",
        r"domicilios\.com",
        r"amazon",
        r"netflix",
        r"spotify",
        r"claro",
        r"tigo",
        r"movistar",
        r"wom\b",
        r"etb\b",
        r"epm\b",
    )
]

NOISE_SUBJECT_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # Advertising and promotions
        r"\bpromo\b",
        r"\boferta\b",
        r"\bdescuento\b",
        r"\bsale\b",
        r"\bdeal\b",
        r"\bunsubscribe\b",
        r"darse de baja",
        r"\bpublicidad\b",
        r"\bbolet[íi]n\b",
        r"\bnewsletter\b",
        r"\bexclusiv[ao]\b",
        r"\baprovecha\b",
        r"\bah[oó]rra\b",
        # Banking and transactional
        r"\bextracto\b",
        r"estado de cuenta",
        r"movimiento.*cuenta",
        r"transacci[oó]n.*realizada",
        r"transacci[oó]n.*exitosa",
        r"pago.*recibido",
        r"pago.*exitoso",
        r"pago.*procesado",
        r"pago.*rechazado",
        r"d[eé]bito.*autom",
        r"transferencia.*realizada",
        r"transferencia.*exitosa",
        r"compra.*aprobada",
        r"compra.*realizada",
        r"recarga.*exitosa",
        r"retiro.*exitoso",
        r"saldo.*disponible",
        r"tu.*cuenta.*blo",
        r"bloqueo.*tarjeta",
        r"nueva.*tarjeta",
        r"tarjeta.*cr[eé]dito",
        # Security and authentication notifications
        r"c[oó]digo.*verificaci[oó]n",
        r"c[oó]digo.*seguridad",
        r"verificaci[oó]n.*cuenta",
        r"\bOTP\b",
        r"alerta.*seguridad",
        r"inicio.*sesi[oó]n",
        r"confirma tu",
        r"confirme su",
        r"your.*account",
        r"\bpassword\b",
        r"\bcontraseña\b",
        # Services / subscriptions / logistics
        r"factura.*#",
        r"invoice.*#",
        r"pedido.*#",
        r"orden.*#",
        r"env[ií]o.*#",
        r"entrega.*paquete",
        r"rastreo.*pedido",
        r"su.*suscripci[oó]n",
        r"renovaci[oó]n.*plan",
        r"membres[ií]a",
        r"\bspam\b",
    )
]

_NOISE_CALENDAR_SUMMARY_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfestivo\b",
        r"\bferiado\b",
        r"\bcumpleaños\b",
        r"\bcumpleanhos\b",
        r"\bbirthday\b",
        r"\bholiday\b",
        r"\bvacacion",
        r"\blicencia\b",
        r"\bpermiso\b",
        r"\bblocked\b",
        r"\bbloqueado\b",
    )
]


def is_noise_email(title: str, sender: str, labels: list[str]) -> bool:
    """Returns True if email is noise based on deterministic heuristics.

    Kept for backward compatibility. For new code, prefer score_non_personal_email().
    """
    score, _ = score_non_personal_email(sender, title, labels, headers=None)
    return score >= 3


def is_noise_calendar(title: str, metadata: dict) -> bool:
    """Returns True if a calendar event is noise (holiday, rejected, personal block)."""
    attendees: list[dict] = metadata.get("attendees") or []

    for att in attendees:
        if att.get("self") and att.get("responseStatus") == "declined":
            return True

    is_all_day: bool = metadata.get("is_all_day", False)
    has_external = any(not att.get("self") for att in attendees)
    if is_all_day and not has_external:
        return True

    if any(p.search(title or "") for p in _NOISE_CALENDAR_SUMMARY_PATTERNS):
        return True

    return False


def is_noise_drive(mime_type: str) -> bool:
    """Returns True if the Drive item is a folder (not evidence)."""
    return (mime_type or "") == "application/vnd.google-apps.folder"


# ── LLM classification prompt ─────────────────────────────────────────────────

WORK_NOISE_SYSTEM_PROMPT = """\
Eres un clasificador de correos y documentos. Tu tarea es separar contenido ÚTIL de RUIDO.

ÚTIL (verdict: "TRABAJO") — conservar:
- Comunicaciones laborales: informes, actas, entregas, reuniones, contratos, aprobaciones, \
  correos con colegas, supervisores o entidades públicas.
- Correos personales importantes: familia, amigos, salud, trámites personales relevantes.
- Documentos de trabajo: archivos, presentaciones, hojas de cálculo relacionadas con actividades.

RUIDO (verdict: "RUIDO") — descartar siempre:
- Publicidad y promociones: ofertas, descuentos, marketing, newsletters.
- Bancario y transaccional: extractos, estados de cuenta, alertas de movimientos, transferencias \
  realizadas, compras aprobadas, recargas, OTPs, códigos de verificación bancaria.
- Notificaciones automáticas de servicios: confirmaciones de pedido, rastreo de envíos, facturas \
  de servicios públicos o plataformas digitales (Netflix, Spotify, Amazon, Rappi, etc.).
- Seguridad de plataformas: alertas de inicio de sesión, cambios de contraseña, verificación de cuenta.
- Spam, boletines, circulares comerciales, suscripciones automáticas.

REGLA CLAVE: Si el correo lo envió un banco, plataforma de pago, e-commerce o servicio de \
suscripción de forma automática → RUIDO, sin excepción.

Responde ÚNICAMENTE con un array JSON válido:
[{"idx": 0, "verdict": "TRABAJO"}, {"idx": 1, "verdict": "RUIDO"}, ...]
"""


def build_work_noise_prompt(items: list[dict]) -> str:
    """Build user prompt for batch work/noise classification."""
    lines = ["Clasifica estos ítems como TRABAJO o RUIDO:\n"]
    for item in items:
        lines.append(
            f"[{item['idx']}] Fuente: {item['source']} | "
            f"Título: {item['title'][:120]} | "
            f"Contenido: {item['content'][:300]}"
        )
    return "\n".join(lines)
