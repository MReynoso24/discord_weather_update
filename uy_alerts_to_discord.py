"""
Monitors INUMET (weather warnings) and posts new/changed alerts to a
Discord channel via webhook.

No official API exists, so this polls INUMET's structured JSON feed
(the same one their Android app uses) and only notifies Discord when
the active advisories actually change.

Install:
    pip install requests

Configure:
    Set DISCORD_WEBHOOK_URL below (or via env var) and run on a schedule
    (cron every 10-15 min, or a GitHub Actions scheduled workflow).

Run once manually to test:
    python uy_alerts_to_discord.py
"""
import os
import json
import hashlib
import logging
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("uy_alerts")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_WEBHOOK_URL_HERE")

# Relative to the current working directory (repo root, both locally and in
# the GitHub Actions runner), so the workflow can find + commit it back.
STATE_FILE = Path("alert_state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UY-Alert-Monitor/1.0; +personal use)"
}

INUMET_JSON_URL = "https://www.inumet.gub.uy/reportes/riesgo/advGral.mch"
INUMET_PAGE_URL = "https://www.inumet.gub.uy/alerta"  # linked in embeds for humans

RISK_HAZARD_LABELS = {
    "riesgoViento": "Viento",
    "riesgoLluvia": "Lluvia",
    "riesgoTormenta": "Tormenta",
    "riesgoVisibilidad": "Visibilidad",
    "riesgoCalor": "Calor",
    "riesgoFrio": "Frío",
}

RISK_LEVEL_NAMES = {2: "Amarilla", 3: "Naranja", 4: "Roja"}
RISK_LEVEL_COLORS = {2: 0xF1C40F, 3: 0xE67E22, 4: 0xE74C3C}  # yellow/orange/red


# ---------------------------------------------------------------------------
# State persistence (so we only alert on *changes*, not every poll)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# INUMET — structured JSON feed used by their own Android app
# ---------------------------------------------------------------------------
def dominant_risk(riesgo_fenomeno: dict) -> tuple[str, int]:
    """Given a riesgoFenomeno dict, return (hazard_label, level) for the
    highest-severity hazard in it."""
    hazard_key, level = max(riesgo_fenomeno.items(), key=lambda kv: kv[1])
    label = RISK_HAZARD_LABELS.get(hazard_key, hazard_key)
    return label, level


def fetch_inumet_json() -> dict:
    resp = requests.get(INUMET_JSON_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def build_inumet_embeds(data: dict) -> list[dict]:
    """Build one Discord embed per active advisory."""
    embeds = []
    for adv in data.get("advertencias", []):
        hazard_label, level = dominant_risk(adv.get("riesgoFenomeno", {}))
        level_name = RISK_LEVEL_NAMES.get(level, f"Nivel {level}")
        color = RISK_LEVEL_COLORS.get(level, 0x95A5A6)

        departamentos = [z["label"] for z in adv.get("zonasArray", [])]
        deptos_str = ", ".join(departamentos) if departamentos else "Ver detalle"

        description = adv.get("descripcion", "").strip()
        if len(description) > 1000:
            description = description[:1000] + "…"

        embeds.append({
            "title": f"⚠️ {adv.get('fenomeno', 'Advertencia meteorológica')} — Alerta {level_name}",
            "description": description,
            "url": INUMET_PAGE_URL,
            "color": color,
            "fields": [
                {"name": "Vigencia", "value": f"{adv.get('comienzo', '?')} → {adv.get('finalizacion', '?')}", "inline": False},
                {"name": "Probabilidad", "value": adv.get("probabilidad", "?"), "inline": True},
                {"name": "Riesgo dominante", "value": f"{hazard_label} ({level_name})", "inline": True},
                {"name": f"Departamentos ({len(departamentos)})", "value": deptos_str[:1024], "inline": False},
            ],
            "footer": {"text": f"Pronosticador: {data.get('pronosticador', '?')} · Actualizado: {data.get('fechaActualizacion', '?')}"},
        })
    return embeds


def check_inumet() -> dict:
    """Returns {'active': bool, 'raw': <hashable summary>, 'embeds': [...]}."""
    data = fetch_inumet_json()

    if data.get("inactivo") or not data.get("advertencias"):
        return {"active": False, "raw": "sin advertencias", "embeds": []}

    embeds = build_inumet_embeds(data)

    # Hashable summary: phenomenon + validity window + risk level per advisory,
    # so a genuinely new/changed advisory triggers a new post, but re-fetching
    # the same bulletin doesn't.
    summary_parts = [
        f"{a.get('fenomeno')}|{a.get('comienzo')}|{a.get('finalizacion')}|{a.get('riesgoFenomeno')}"
        for a in data["advertencias"]
    ]
    return {"active": True, "raw": "||".join(summary_parts), "embeds": embeds}


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def post_embeds_to_discord(embeds: list[dict], mention_everyone: bool = False) -> None:
    """Post up to 10 embeds in one webhook message (Discord's per-message cap).

    If mention_everyone is True, "@everyone" is prepended as plain content
    on the FIRST chunk only (so it pings once, not once per 10 embeds).
    """
    if not DISCORD_WEBHOOK_URL or "PASTE_YOUR" in DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not configured; skipping post. Embeds were:\n%s", embeds)
        return

    for chunk_start in range(0, len(embeds), 10):
        chunk = embeds[chunk_start:chunk_start + 10]
        payload = {"embeds": chunk}
        if mention_everyone and chunk_start == 0:
            payload["content"] = "@everyone"
            # Webhooks need this explicitly, or Discord silently strips the
            # mention and it shows as plain text instead of pinging.
            payload["allowed_mentions"] = {"parse": ["everyone"]}
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
    log.info("Posted %d embed(s) to Discord.", len(embeds))


def post_simple_to_discord(title: str, description: str, url: str, color: int, mention_everyone: bool = False) -> None:
    embed = {"title": title, "description": description[:3900], "url": url, "color": color}
    post_embeds_to_discord([embed], mention_everyone=mention_everyone)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    state = load_state()

    # --- INUMET ---
    try:
        inumet = check_inumet()
        h = content_hash(inumet["raw"])
        if state.get("inumet_hash") != h:
            state["inumet_hash"] = h
            if inumet["active"]:
                post_embeds_to_discord(inumet["embeds"], mention_everyone=True)
            else:
                # Only announce the "all clear" if we previously had an active warning
                if state.get("inumet_was_active"):
                    post_simple_to_discord(
                        title="✅ Advertencia meteorológica finalizada — INUMET",
                        description="No hay advertencias meteorológicas vigentes.",
                        url=INUMET_PAGE_URL,
                        color=0x2ECC71,  # green
                        mention_everyone=True,
                    )
            state["inumet_was_active"] = inumet["active"]
        else:
            log.info("INUMET: no change.")
    except Exception:
        log.exception("Error checking INUMET")

    save_state(state)


if __name__ == "__main__":
    main()
