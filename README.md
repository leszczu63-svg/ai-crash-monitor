# AI Crash Monitor

Monitoruje 6 sygnałów krachu AI. Wysyła powiadomienia na Telegram o każdym nowym zdarzeniu i alarm,
gdy **≥3 różne zdarzenia zmieszczą się w jednym oknie 90 dni**. Działa za darmo:
GitHub Actions (harmonogram) + GitHub Pages (dashboard) + powiadomienia push **ntfy.sh** (i/lub bot Telegram).

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

## Uruchomienie – powiadomienia ntfy (zalecane, bez konta)

1. Zainstaluj aplikację **ntfy** na telefonie (Google Play / App Store).
2. Wymyśl długą, trudną do zgadnięcia nazwę tematu, np. `ai-krach-7f3k9q2xm81z` – kto ją zna, może czytać powiadomienia.
3. W aplikacji: **+** → *Subscribe to topic* → wpisz tę nazwę (serwer domyślny `ntfy.sh`).
4. GitHub → Settings → Secrets and variables → Actions → **Repository secrets** → `NTFY_TOPIC` = ta nazwa.
5. Actions → „Test Telegrama” (testuje wszystkie kanały) → *Run workflow* → powinno przyjść powiadomienie.
6. Actions → „Ocena dzienna” → *Run workflow* → przyjdzie powitanie ze statusem.

Pages: Settings → Pages → *Deploy from a branch* → `main` / `/docs`. Dashboard: `https://<login>.github.io/<repo>/`

### Opcjonalnie: Telegram
Sekrety `TELEGRAM_BOT_TOKEN` (od `@BotFather`) i `TELEGRAM_CHAT_ID` (od `@userinfobot`).
Jeśli Telegram zwraca 429 „Too Many Requests” – usuń oba sekrety, inaczej przebiegi będą czerwone.

### Komendy
- Telegram: wyślij komendę botowi (odpowiedź do ~1 h).
- Bez Telegrama: Actions → **„Komenda”** → *Run workflow* → wpisz np. `status`, `kandydaci`,
  `potwierdz ID`, `odrzuc ID`, `dodaj bankrupt 2027-01-15`, `usun acquired 2027-01-15`, `h100 0.95`.
  Działa też z aplikacji GitHub na telefonie. Odpowiedź przychodzi przez ntfy.

## Harmonogram
- `daily.yml` – pn–pt 22:30 UTC (po zamknięciu sesji w USA) + codziennie 12:00 UTC (H100).
- `commands.yml` – co godzinę: komendy z Telegrama i skan newsów.

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
