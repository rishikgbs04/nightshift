# Put nightshift in the cloud (one time, about 20 minutes)

After this, nightshift runs every night on its own and you only use the app on
your phone. You need a laptop for these steps once.

## 1. Create the server

```bash
brew install flyctl
fly auth signup                      # or: fly auth login
cd nightshift
```

Open `fly.toml` and change `app = "nightshift-change-me"` to something unique,
like `nightshift-yourname`. Then:

```bash
fly apps create nightshift-yourname
fly volumes create nightshift_data --size 1 --region ewr --yes
fly secrets set ANTHROPIC_API_KEY=sk-ant-... NIGHTSHIFT_PASSWORD='pick-a-long-password'
fly deploy
```

`fly deploy` takes a few minutes the first time. When it finishes, your app is at
`https://nightshift-yourname.fly.dev`.

The machine runs all month, so Fly bills it continuously; check fly.io/pricing
for the current cost of a 1 GB shared machine and a 1 GB volume.

## 2. Load your details (in the browser on your laptop)

Open `https://nightshift-yourname.fly.dev`, sign in with your password, and go to
**Settings** (the gear icon).

1. **Your resume (profile.yaml):** paste the contents of your local
   `profile.yaml` and save.
2. **Your answers to application questions:** fill in every blank field
   (work authorization, sponsorship, salary, start date...). Blank ones are
   never guessed; jobs that ask them wait for you.
3. **Settings (config.yaml):** set at least
   ```yaml
   companies:
     include_defaults: true
   search:
     needs_sponsorship: true      # or false
   notify:
     ntfy_topic: nightshift-yourname-k3j9x2   # long and random
     app_url: https://nightshift-yourname.fly.dev
   ```
   and, when you're ready for autopilot, the `pipeline` and `submit` settings
   from the README.
4. Press **Run now** once to make sure everything works. Your first jobs show
   up in the app when it finishes.

## 3. Set up your phone

1. Open `https://nightshift-yourname.fly.dev` in Safari (iPhone) or Chrome
   (Android) and sign in.
2. iPhone: Share, then **Add to Home Screen**. Android: menu, then
   **Install app**. It now opens like an app.
3. Install **ntfy** from the App Store or Play Store and subscribe to the
   `ntfy_topic` you chose. Tap **Send a test notification** in Settings to
   check.

That's it. Every night at 2:17 AM it finds, scores and tailors new jobs; in
the morning you get a notification, open the app, and swipe.

## Updating later

```bash
git pull && fly deploy
```

Your data lives on the volume, so updates never touch it.
