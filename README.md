# AI Crash Monitor

Monitoruje 6 sygnałów krachu AI. Wysyła powiadomienia na Telegram o każdym nowym zdarzeniu i alarm,
gdy **≥3 różne zdarzenia zmieszczą się w jednym oknie 90 dni**. Działa za darmo:
GitHub Actions (harmonogram) + GitHub Pages (dashboard) + bot Telegram.

| Zdarzenie | Źródło | Warunek |
|---|---|---|
| NVDA | Yahoo Finance (yfinance) | zamknięcie ≥50% poniżej ATH |
| SOXX | Yahoo Finance | zamknięcie ≥40% poniżej ATH |
| Bankructwo OpenAI/Anthropic | Google News RSS → potwierdzenie w Telegramie | potwierdzone |
| Przejęcie OpenAI | Google News RSS → potwierdzenie w Telegramie | potwierdzone |
| H100 | SiliconData Silicon Index (SDH100RT, strona publiczna) | ≤ 1,00 USD/h przez 5 kolejnych dni |
| Sprzęt AI | Yahoo Finance: TSM, ASML, AVGO, ANET, SMCI | którakolwiek ≥50% poniżej ATH |

ATH liczone z cen zamknięcia skorygowanych o splity (bez korekty o dywidendy).
Dodatkowo: wczesne ostrzeżenia przy 80% progu (np. NVDA −40%) i alert, gdy źródło danych nie działa 2× z rzędu.

## Uruchomienie (ok. 10 minut)

1. **Bot Telegram:** w Telegramie napisz do `@BotFather` → `/newbot` → zapisz token.
   Potem napisz cokolwiek do swojego nowego bota.
2. **Chat ID:** lokalnie
   `pip install -r requirements.txt && TELEGRAM_BOT_TOKEN=xxx python monitor.py chatid`
3. **Repozytorium:** utwórz **publiczne** repo na GitHubie i wypchnij ten katalog
   (publiczne = darmowe Pages i bez limitu minut Actions; w repo nie ma żadnych sekretów).
4. **Sekrety:** Settings → Secrets and variables → Actions → `TELEGRAM_BOT_TOKEN` i `TELEGRAM_CHAT_ID`.
5. **Pages:** Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.
6. **Start:** Actions → „Ocena dzienna” → *Run workflow*. Dostaniesz wiadomość powitalną ze statusem.

Dashboard: `https://<login>.github.io/<repo>/`

## Harmonogram
- `daily.yml` – pn–pt 22:30 UTC (po zamknięciu sesji w USA) + codziennie 12:00 UTC (H100).
- `commands.yml` – co godzinę: komendy z Telegrama i skan newsów.

## Komendy w Telegramie
`/status`, `/kandydaci`, `/potwierdz ID [data]`, `/odrzuc ID`, `/dodaj bankrupt|acquired RRRR-MM-DD`,
`/usun …`, `/h100 CENA [data]`, `/test`, `/pomoc`. Bot odpowiada przy najbliższym przebiegu (do ~1 h).

## Konfiguracja (`config.json`)
- `window_days` / `min_events` – okno 90 dni, 3 zdarzenia.
- `deadline` – opcjonalna data graniczna (np. `"2027-12-31"`); `null` = brak.
- `count_mode`:
  - `"crossing"` (domyślnie) – datą zdarzenia jest dzień **przekroczenia** progu. Ponowne przekroczenie
    liczy się dopiero po odbiciu o `rearm_margin` (5 pp) powyżej progu.
  - `"daily"` – zdarzenie „trwa” w każdym dniu, w którym warunek jest spełniony.
    Uwaga: SMCI jest ~79% pod ATH od 2024 r., więc w tym trybie „Sprzęt AI” byłby stale aktywny.
- Progi, tickery, wczesne ostrzeżenia, frazy newsów – w tym samym pliku.

## Testy
`python tests/test_logic.py`
