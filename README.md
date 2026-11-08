# Padhan Video Engine
Free, fully automatic YouTube pipeline (runs on GitHub Actions, no laptop, no Claude usage).

topic (Gemini) -> fact sheet (Gemini + Google Search) -> script -> independent review by a second model
(fact-check + hook/retention scores, max 2 rewrite rounds) -> **quality gate** -> images (Pollinations)
-> narration (Kokoro, open-source) -> edit (camera motion, captions, titles, music) -> thumbnail -> upload long + Short.

* Schedule & channels: `channels.json` (`days`: 0=Mon … 6=Sun)
* History of published videos: `data/`
* Every run keeps the finished videos for 7 days under Actions → run → Artifacts.
* Manual test: Actions → daily-videos → Run workflow → channel `forgotten-empires`, no_upload `true`.

Secrets: `GEMINI_API_KEY`, `POLLINATIONS_API_KEY`, `YT_ALL` (combined YouTube credentials). Legacy per-channel secrets also work: (Settings → Secrets and variables → Actions): `GEMINI_API_KEY`, `POLLINATIONS_API_KEY`,
`YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_TOKEN_FORGOTTEN_EMPIRES`, `YT_TOKEN_CURIOSITY_SHORTS`,
`YT_TOKEN_BUSINESS_FRONTIER`, `YT_TOKEN_BYTE_BRIEF` (paste the whole `.youtube-token.json` content).
