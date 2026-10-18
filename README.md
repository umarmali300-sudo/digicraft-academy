# Digicraft

This folder is the Digicraft academy website. The Python package and environment variable names still use the `nexora` prefix so existing local configuration keeps working. It does not replace the Mark XXXIX assistant in the parent project.

The site is a FastAPI application with server-rendered pages and a SQLite database. Passwords are stored as PBKDF2-HMAC-SHA256 hashes. Sessions use an HttpOnly cookie. Member features require a verified `@gmail.com` address.

## Run locally

```bash
cd nexora-academy
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000

Copy `.env.example` to `.env` only if you set variables yourself. The app reads environment variables; it does not load `.env` automatically.

On a localhost base URL, verification and password-reset links are appended to `data/email.log` when SMTP is not configured. That is local development only. It is not production email delivery.

## What works without extra credentials

- Home, courses, FAQ, consultation, legal pages, and the three published learning guides
- Gmail and password registration, login, logout, email verification, and password reset
- Profile name, profile photo, and account deletion
- Private chat for verified members, with reports stored for review
- Course, lesson, enrollment, and progress storage when a published course exists
- Plan requests that stay unpaid until a verified Stripe webhook arrives
- Consultation requests saved in the database

No courses are published yet, so the courses page shows: “New courses are being prepared. Check back soon.”

## Admin dashboard

Open `/admin` after signing in with a verified Gmail account whose address is listed in `NEXORA_ADMIN_EMAILS`. An unverified account is not treated as an admin. Separate addresses with commas. Restart the site after changing it.

There is no admin password in the website code and no public admin signup. If the variable is empty, no account can open the dashboard. Students who are not listed are redirected away from admin pages. Admin APIs return 401 when signed out and 403 when the account is not an admin.

## Configuration still required

Google sign-in does not complete until these are set:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI` (default `http://127.0.0.1:8000/auth/google/callback`)

Add that exact redirect URI to the Google Cloud OAuth web client.

Real email delivery needs:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_TLS`

Set `NEXORA_DEV_LOG_EMAILS=0` in production so reset links are not written to disk.

Direct consultation links stay hidden until the relevant value is set:

- `NEXORA_WHATSAPP_NUMBER`
- `NEXORA_TELEGRAM_USERNAME`
- `NEXORA_CONTACT_EMAIL`
- `NEXORA_ZOOM_URL`
- `NEXORA_MEET_URL`

Checkout is not offered unless both of these exist:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`

Point Stripe’s webhook to `/payments/stripe/webhook`. An order is marked paid only when that webhook has a valid signature and `payment_status` is `paid`. The return URL never marks a payment complete.

Optional:

- `NEXORA_SECRET_KEY` — otherwise a key is created in `data/.secret`
- `NEXORA_DATABASE_PATH`
- `NEXORA_BASE_URL`
- `NEXORA_COOKIE_SECURE=1` behind HTTPS
- `NEXORA_ADMIN_EMAILS` — comma-separated canonical Gmail addresses that can open `/moderation`
- `NEXORA_TRUST_PROXY=1` only behind a trusted proxy

## Tests

```bash
python -m pytest
```
