# Fitmeezo — Project Context & Production Configuration

## Production Website
- **Primary Domain**: `fitmeezo.com`
- **Primary URL**: `https://fitmeezo.com`
- **Alternate / WWW Domain**: `https://www.fitmeezo.com`
- **Hosting Platform**: Render (`https://fitmeezo-website.onrender.com`)
- **Brand Name**: Fitmeezo
- **Tagline**: SHOP SMART • LIVE BETTER
- **Target Market**: India (Currency: INR ₹, Timezone: Asia/Kolkata)

## Core Domains & Allowed Hosts
Whenever configuring redirects, email templates, OAuth callbacks, password reset links, or security settings:
- Always use `fitmeezo.com` as the default production site domain.
- `ALLOWED_HOSTS`: `fitmeezo.com`, `www.fitmeezo.com`, `fitmeezo-website.onrender.com`, `127.0.0.1`, `localhost`
- `CSRF_TRUSTED_ORIGINS`: `https://fitmeezo.com`, `https://www.fitmeezo.com`, `http://fitmeezo.com`, `http://www.fitmeezo.com`, `https://fitmeezo-website.onrender.com`

## OAuth & Social Login
- Google OAuth Redirect URI: `https://fitmeezo.com/accounts/google/login/callback/`
