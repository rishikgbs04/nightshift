# Put nightshift on a free server

After this, nightshift runs on its own: it searches and tailors every night,
checks your inbox for recruiter replies every two hours, and emails recruiters
and alumni. You only use the app on your phone.

This guide uses **Oracle Cloud's Always Free tier** ($0) and **Tailscale**
(free) for a private HTTPS link to your phone. Budget about an hour the first
time. You need your laptop once.

Running cost: $0 for the server, plus the AI cap you set (`monthly_budget_usd`,
default $9).

---

## 1. Get your keys (in the browser)

| Key | Where | Needed for |
|---|---|---|
| Anthropic API key | console.anthropic.com → API Keys (add $5–10 credit under Billing, and set a monthly limit under Limits) | AI tailoring |
| Email app password | myaccount.google.com/apppasswords (turn on 2-Step Verification first) | reading replies, sending outreach |
| Hunter.io API key (optional) | hunter.io → sign up free → API | finding recruiter emails |
| An app password for nightshift | make one up, long | signing in on your phone |

**NYU email:** NYU's Google accounts may not offer app passwords. If
myaccount.google.com/apppasswords says the setting isn't available, use your
personal Gmail for `email.address` instead. Everything else stays the same.

### Using Google AI Pro credits instead (Gemini, $0 out of pocket)

Google AI Pro includes $10/month of Google Cloud credits that pay for the
Gemini API (claim them at developers.google.com/program/my-benefits). To use
them:

1. In **aistudio.google.com → Get API key → Create API key**, pick the Google
   Cloud project linked to the billing account the credits went to (for a new
   account that's "My First Project"). Keep the key private.
2. Use `GEMINI_API_KEY=...` in your secrets instead of `ANTHROPIC_API_KEY`.
3. In Settings → config.yaml, replace the `llm:` section with:
   ```yaml
   llm:
     provider: openai
     base_url: https://generativelanguage.googleapis.com/v1beta/openai
     api_key_env: GEMINI_API_KEY
     model: gemini-3.5-flash-lite          # tailoring and cover letters
     task_models:                          # cheaper model for the high-volume work
       score: gemini-3.1-flash-lite
       classify: gemini-3.1-flash-lite
       answers: gemini-3.1-flash-lite
       outreach: gemini-3.1-flash-lite
     prices:                               # USD per million tokens, paid tier
       gemini-3.5-flash-lite: [0.30, 2.50]
       gemini-3.1-flash-lite: [0.25, 1.50]
     reasoning_effort: low                 # thinking is billed as output
     max_tokens: 8000
     monthly_budget_usd: 12                # stay under your credits
   pipeline:
     max_llm_scoring_per_run: 40
     max_tailor_per_run: 15
   ```
4. In the Google Cloud console, create a **budget alert** (Billing → Budgets &
   alerts) of $1 with credits included, so you'd hear about any charge the
   credits don't cover.

Roughly: $12/month covers about 15 tailored applications a day plus scoring,
replies and outreach. Prices change; check ai.google.dev/gemini-api/docs/pricing
and update `prices` if they do.

## 2. Create the free server

1. Sign up at **cloud.oracle.com** (Free Tier). It asks for a card to verify
   you; Always Free resources aren't charged. Pick a home region near you,
   e.g. **US East (Ashburn)**. You can't change it later.
2. Go to **Compute → Instances → Create instance**.
   - **Image:** Canonical Ubuntu 24.04
   - **Shape:** Ampere → `VM.Standard.A1.Flex`, **2 OCPUs, 12 GB memory**
     (the Always Free maximum)
   - **SSH keys:** "Generate a key pair for me" → **download the private key**
   - Click **Create**.

   If you see **"Out of capacity"**, try another availability domain in the
   same screen, or try again in an hour. Free Ampere capacity is in demand.
3. When it's running, copy the **Public IP address** from the instance page.

**Keep it from being reclaimed.** Oracle may reclaim Always Free instances that
look idle for 7 days (CPU, network and memory all under 20%), and nightshift is
quiet most of the day. Many people avoid this by upgrading the account to
**Pay As You Go** (Billing → Upgrade). Oracle states Always Free resources stay
free after upgrading. If you upgrade, also create a **budget alert** of $1
(Billing → Budgets) so you'd know about any charge.

## 3. Connect to the server (Terminal on your Mac)

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@YOUR_PUBLIC_IP
```

Type `yes` the first time. Everything below runs **on the server**.

## 4. Install Docker and nightshift

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```

Connect again (same `ssh` command), then:

```bash
git clone https://github.com/rishikgbs04/nightshift.git
cd nightshift
docker build -t nightshift .
```

The build takes 5–10 minutes the first time.

## 5. Add your secrets

```bash
nano ~/nightshift.env
```

Paste this, fill in your values, then press Ctrl+O, Enter, Ctrl+X:

```
ANTHROPIC_API_KEY=sk-ant-...
NIGHTSHIFT_PASSWORD=your-long-app-password
NIGHTSHIFT_EMAIL_PASSWORD=your-16-letter-google-app-password
HUNTER_API_KEY=your-hunter-key
TZ=America/New_York
```

```bash
chmod 600 ~/nightshift.env
```

## 6. Start it

```bash
docker run -d --name nightshift --restart unless-stopped \
  --env-file ~/nightshift.env \
  -v ~/nightshift-data:/data \
  -p 127.0.0.1:8080:8080 \
  nightshift
```

Check it's up:

```bash
curl -s localhost:8080/healthz
```

It should print `{"ok":true}`. It restarts by itself after crashes and reboots.

## 7. A private HTTPS link for your phone (Tailscale)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the link it prints and sign in (Google login works). Then:

```bash
sudo tailscale serve --bg 8080
```

If it asks you to enable HTTPS, open the link it prints, turn it on, and run
the command again. It prints your app's address, like
`https://nightshift.tail1234.ts.net`. Only devices signed in to your Tailscale
account can open it.

On your phone:
1. Install **Tailscale** from the App Store or Play Store and sign in with the
   same account.
2. Open the address in Safari (or Chrome on Android) and sign in with your
   `NIGHTSHIFT_PASSWORD`.
3. Safari: Share → **Add to Home Screen**. Android: menu → **Install app**.

Would rather not keep Tailscale on your phone? Run
`sudo tailscale funnel --bg 8080` instead of `serve`. That makes the address
public on the internet, protected by your app password.

## 8. Fill in your details (in the app)

Open **Settings** (gear icon):

1. **Your answers to application questions:** fill in every field. Blank ones
   are never guessed.
2. **Your resume (profile.yaml):** paste your full `profile.yaml` from your Mac.
   Include more than fits on a page.
3. **Settings (config.yaml):** set at least:
   ```yaml
   companies:
     include_defaults: true
   search:
     needs_sponsorship: true          # or false
   notify:
     ntfy_topic: nightshift-rishik-k3j9x2   # long and random
   email:
     address: bg2960@nyu.edu          # or your Gmail
     from_name: Rishik Gannavarapu
   outreach:
     enabled: true
     signature: "linkedin.com/in/rishik-gannavarapu | github.com/rishikgbs04"
   ```
4. Install **ntfy** on your phone and subscribe to your `ntfy_topic`. Tap
   **Send a test notification** in Settings to check.
5. Tap **Check replies now** (Email panel) to confirm the inbox connection,
   then **Run now** to do the first search.

When you trust what it produces, turn on submitting (`submit: enabled: true`,
then later `live: true`) and auto-approve (`pipeline: auto_approve_min_score: 8`).

---

## Updating later

```bash
ssh -i ~/Downloads/ssh-key-*.key ubuntu@YOUR_PUBLIC_IP
cd nightshift && git pull && docker build -t nightshift .
docker rm -f nightshift
```

Then run the `docker run` command from step 6 again. Your data lives in
`~/nightshift-data`, so updates never touch it.

## If something goes wrong

```bash
docker logs --tail 100 nightshift
```

Paste the output to whoever is helping you.

---

## Alternative: Fly.io (about $7/month, simpler)

No Oracle account, no Tailscale; the app gets a public HTTPS address.

```bash
brew install flyctl && fly auth signup
fly apps create nightshift-yourname          # then set the same name in fly.toml
fly volumes create nightshift_data --size 1 --region ewr --yes
fly secrets set ANTHROPIC_API_KEY=... NIGHTSHIFT_PASSWORD=... NIGHTSHIFT_EMAIL_PASSWORD=... HUNTER_API_KEY=...
fly deploy
```

Then follow step 8 at `https://nightshift-yourname.fly.dev`.
