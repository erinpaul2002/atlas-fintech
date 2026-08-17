"""Small, self-contained pages for the Google OAuth browser handoff."""

from html import escape

from fastapi.responses import HTMLResponse


TELEGRAM_URL = "https://t.me/AtlasEPMBot"


def oauth_page(
    *,
    title: str,
    eyebrow: str,
    heading: str,
    body: str,
    tone: str,
    status_code: int = 200,
    show_services: bool = False,
    action_label: str = "Return to Atlas",
) -> HTMLResponse:
    """Render a branded OAuth result without external assets or scripts."""
    safe_tone = "success" if tone == "success" else "error"
    services = """
        <ul class="services" aria-label="Connected Google services">
          <li><span>G</span>Gmail</li>
          <li><span>C</span>Calendar</li>
          <li><span>S</span>Sheets</li>
          <li><span>D</span>Drive</li>
        </ul>
    """ if show_services else ""

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#07110f">
  <meta name="color-scheme" content="dark">
  <title>{escape(title)} · Atlas</title>
  <style>
    :root {{
      --ink: #07110f;
      --panel: rgba(14, 29, 25, .92);
      --line: rgba(190, 255, 213, .14);
      --text: #f2f7f3;
      --muted: #9fb1a8;
      --signal: #a7f46a;
      --signal-deep: #68b837;
      --error: #ff9c84;
      --display: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, Georgia, serif;
      --body: "Avenir Next", Avenir, "Segoe UI Variable", "Trebuchet MS", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ min-height: 100%; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      overflow-x: hidden;
      color: var(--text);
      font-family: var(--body);
      background:
        radial-gradient(circle at 18% 14%, rgba(98, 179, 102, .18), transparent 30rem),
        radial-gradient(circle at 82% 86%, rgba(64, 119, 89, .14), transparent 26rem),
        var(--ink);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .32;
      background-image:
        linear-gradient(var(--line) 1px, transparent 1px),
        linear-gradient(90deg, var(--line) 1px, transparent 1px);
      background-size: 48px 48px;
      mask-image: linear-gradient(to bottom, black, transparent 88%);
    }}
    .shell {{
      position: relative;
      width: min(92vw, 760px);
      margin: 32px auto;
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: clamp(28px, 6vw, 64px);
      background: var(--panel);
      box-shadow: 0 34px 100px rgba(0, 0, 0, .38), inset 0 1px rgba(255, 255, 255, .04);
      backdrop-filter: blur(18px);
      animation: arrive .65s cubic-bezier(.2, .8, .2, 1) both;
    }}
    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin-bottom: clamp(42px, 8vw, 72px);
      color: #dce8df;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .22em;
    }}
    .brand-mark {{
      width: 22px;
      aspect-ratio: 1;
      border: 1px solid rgba(167, 244, 106, .5);
      border-radius: 7px 7px 7px 2px;
      background: linear-gradient(145deg, rgba(167, 244, 106, .35), rgba(167, 244, 106, .04));
      box-shadow: inset 0 0 0 4px var(--ink);
    }}
    .status {{
      display: flex;
      align-items: center;
      gap: 18px;
      margin-bottom: 24px;
    }}
    .status-icon {{
      position: relative;
      flex: 0 0 auto;
      width: 48px;
      height: 48px;
      border: 1px solid rgba(167, 244, 106, .45);
      border-radius: 50%;
      background: rgba(167, 244, 106, .09);
      box-shadow: 0 0 0 8px rgba(167, 244, 106, .035);
    }}
    [data-tone="success"] .status-icon::after {{
      content: "";
      position: absolute;
      width: 16px;
      height: 8px;
      left: 14px;
      top: 16px;
      border-left: 3px solid var(--signal);
      border-bottom: 3px solid var(--signal);
      transform: rotate(-45deg);
    }}
    [data-tone="error"] .status-icon {{
      border-color: rgba(255, 156, 132, .5);
      background: rgba(255, 156, 132, .09);
      box-shadow: 0 0 0 8px rgba(255, 156, 132, .035);
    }}
    [data-tone="error"] .status-icon::before,
    [data-tone="error"] .status-icon::after {{
      content: "";
      position: absolute;
      top: 22px;
      left: 14px;
      width: 19px;
      height: 3px;
      border-radius: 3px;
      background: var(--error);
      transform: rotate(45deg);
    }}
    [data-tone="error"] .status-icon::after {{ transform: rotate(-45deg); }}
    .eyebrow {{
      margin: 0;
      color: var(--signal);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .17em;
      text-transform: uppercase;
    }}
    [data-tone="error"] .eyebrow {{ color: var(--error); }}
    h1 {{
      max-width: 590px;
      margin: 0 0 18px;
      font-family: var(--display);
      font-size: clamp(44px, 8vw, 76px);
      font-weight: 400;
      letter-spacing: -.045em;
      line-height: .98;
    }}
    .lede {{
      max-width: 560px;
      margin: 0;
      color: var(--muted);
      font-size: clamp(16px, 2.4vw, 19px);
      line-height: 1.65;
    }}
    .services {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 9px;
      padding: 0;
      margin: 34px 0 0;
      list-style: none;
    }}
    .services li {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      color: #cbd9d0;
      font-size: 12px;
      font-weight: 700;
      background: rgba(255,255,255,.025);
    }}
    .services span {{
      display: grid;
      place-items: center;
      width: 20px;
      height: 20px;
      border-radius: 6px;
      color: var(--ink);
      font-size: 10px;
      background: var(--signal);
    }}
    .action-row {{
      display: flex;
      align-items: center;
      gap: 20px;
      margin-top: 38px;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      min-height: 50px;
      padding: 0 22px;
      border-radius: 13px;
      color: #0a150f;
      font-size: 14px;
      font-weight: 850;
      text-decoration: none;
      background: var(--signal);
      box-shadow: 0 12px 34px rgba(109, 187, 55, .18);
      transition: transform .18s ease, background .18s ease, box-shadow .18s ease;
    }}
    [data-tone="error"] .button {{ color: #24100c; background: var(--error); }}
    .button:hover {{
      transform: translateY(-2px);
      background: #bdff8c;
      box-shadow: 0 16px 42px rgba(109, 187, 55, .26);
    }}
    [data-tone="error"] .button:hover {{ background: #ffb6a4; }}
    .button::after {{ content: "↗"; font-size: 16px; }}
    .aside {{ color: #71867a; font-size: 12px; line-height: 1.5; }}
    .security {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 48px;
      color: #60766a;
      font-size: 10px;
      font-weight: 800;
      letter-spacing: .14em;
      text-transform: uppercase;
    }}
    .security::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--signal-deep);
      box-shadow: 0 0 0 4px rgba(104, 184, 55, .09);
    }}
    @keyframes arrive {{
      from {{ opacity: 0; transform: translateY(18px) scale(.985); }}
      to {{ opacity: 1; transform: none; }}
    }}
    @media (max-width: 620px) {{
      .shell {{ border-radius: 22px; padding: 28px 24px 32px; }}
      .brand {{ margin-bottom: 46px; }}
      .services {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .action-row {{ align-items: flex-start; flex-direction: column; }}
      .button {{ width: 100%; }}
      .security {{ margin-top: 36px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ animation: none !important; transition: none !important; }}
    }}
  </style>
</head>
<body>
  <main class="shell" data-tone="{safe_tone}" aria-labelledby="page-title">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span>ATLAS</div>
    <div class="status">
      <span class="status-icon" aria-hidden="true"></span>
      <p class="eyebrow">{escape(eyebrow)}</p>
    </div>
    <h1 id="page-title">{escape(heading)}</h1>
    <p class="lede">{escape(body)}</p>
    {services}
    <div class="action-row">
      <a class="button" href="{TELEGRAM_URL}">{escape(action_label)}</a>
      <span class="aside">You can safely close this tab.</span>
    </div>
    <div class="security">Secure Google connection</div>
  </main>
</body>
</html>"""
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def connected_page() -> HTMLResponse:
    return oauth_page(
        title="Google connected",
        eyebrow="Connection complete",
        heading="You're connected.",
        body="Atlas can now work with your Google workspace when you ask—without turning your chat into a setup screen.",
        tone="success",
        show_services=True,
    )


def expired_page() -> HTMLResponse:
    return oauth_page(
        title="Connection link expired",
        eyebrow="Fresh link needed",
        heading="This link timed out.",
        body="Return to Atlas and ask to connect Google again. You'll get a new secure, one-time link.",
        tone="error",
        status_code=400,
        action_label="Get a new link",
    )


def cancelled_page() -> HTMLResponse:
    return oauth_page(
        title="Connection cancelled",
        eyebrow="Nothing was changed",
        heading="Connection cancelled.",
        body="Your Google account was not connected. You can return to Atlas and try again whenever you're ready.",
        tone="error",
        status_code=400,
        action_label="Return to Atlas",
    )


def failed_page() -> HTMLResponse:
    return oauth_page(
        title="Google connection failed",
        eyebrow="We couldn't finish",
        heading="Google didn't connect.",
        body="Return to Atlas and request a fresh connection link. Your existing Google data was not changed.",
        tone="error",
        status_code=502,
        action_label="Try from Atlas",
    )
