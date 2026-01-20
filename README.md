# Couple Finance Bot 🤖💰

Slack bot for Jacob & Naomi to track income, expenses, and fund balances.

## Commands

| Command | Example | Description |
|---------|---------|-------------|
| Log income | `jacob 2.8M salary` | Log Jacob's salary |
| Log income | `naomi 5M commission` | Log Naomi's commission |
| Log expense | `joint 500K groceries` | Log joint expense |
| Log for self | `2.8M salary` | Logs for whoever sent the message |
| Check status | `status` | See fund balances & monthly summary |
| Get help | `help` | Show all commands |

## Amount Formats

- `2.8M` = ₩2,800,000
- `500K` = ₩500,000
- `2800000` = ₩2,800,000

## Environment Variables

```
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-signing-secret
GOOGLE_SHEET_ID=1CRWaO855R-_8GKR2Pwpqw28ZFWKAMVAY4F6ZhFfEnbQ
GOOGLE_CREDENTIALS={"type":"service_account",...}
```

## Deploy to Railway

1. Push this code to GitHub
2. Connect Railway to your GitHub repo
3. Add environment variables in Railway dashboard
4. Deploy!
