# Project Overview
I want to build an automated system that checks arXiv daily for new papers in my specific areas of interest, filters them by keyword, and sends me an alert on Telegram. The system must run autonomously using GitHub Actions.

## Tech Stack
*   **Language:** Python 3.9+
*   **Libraries:** `arxiv` (for fetching papers), `requests` (for Telegram API), `datetime` (for time filtering)
*   **Automation:** GitHub Actions
*   **Delivery:** Telegram Bot API

## Domain Specifications & Filtering
The script should fetch papers from the last 24 hours in the following arXiv categories:
1.  `physics.plasm-ph` (Plasma Physics)
2.  `physics.comp-ph` (Computational Physics)
3.  `q-fin.CP` (Computational Finance)
4.  `q-fin.PR` (Pricing of Securities)

It should only send an alert if the paper's title or abstract contains at least one of the following keywords (case-insensitive):
*   **Physics/Computing:** AMReX, Grad-Shafranov, EFIT++, VMEC, nuclear fusion, plasma simulation, high-performance computing
*   **Finance:** stochastic calculus, volatility modeling, GARCH, Heston, Merton, derivatives pricing, arbitrage

## Tasks to Execute
Please generate the complete codebase by completing the following steps:

1.  **Create `requirements.txt`**: Include `arxiv` and `requests`.
2.  **Create `main.py`**:
    *   Implement a function to fetch papers from the specified arXiv categories submitted within the last 24 hours.
    *   Implement a filtering function that checks titles and abstracts against the specified keywords.
    *   Implement a `send_telegram_message` function using the standard Telegram Bot API endpoint (`https://api.telegram.org/bot<TOKEN>/sendMessage`).
    *   Format the Telegram message cleanly using Markdown (bolding the title, providing a snippet of the abstract, and including the arxiv URL).
    *   Ensure environmental variables (`TELEGRAM_TOKEN` and `CHAT_ID`) are used for secrets. Do not hardcode them.
3.  **Create `.github/workflows/daily_papers.yml`**:
    *   Set up a GitHub Actions workflow that triggers on a CRON schedule (e.g., every day at 08:00 UTC) and via `workflow_dispatch` (for manual testing).
    *   The workflow should check out the code, set up Python, install dependencies, and run `main.py`.
    *   Map the GitHub repository secrets to the environment variables required by the Python script.
4.  **Create `README.md`**: Provide brief instructions on how to set up the Telegram bot via @BotFather, retrieve the Chat ID, and add them to GitHub Repository Secrets.

## Code Quality Requirements
*   Add clear comments and error handling (e.g., if the Telegram API fails or arXiv is down).
*   Ensure the script cleanly handles edge cases, such as no new papers matching the criteria on a given day (it should just exit quietly without sending a blank message).