# Lead Icebreaker Generator

This project includes:

- a Python CLI that processes a leads CSV
- a FastAPI server for single-row API requests
- bearer-token auth for your endpoint
- structured JSON error responses
- a Dockerfile for local deployment or hosting

## Expected CSV headers

Your input file should include exactly these columns:

- `Business Name`
- `Niche`
- `City`
- `Website`
- `Domain`
- `Name`
- `First Name`
- `Surname`
- `Email`

## What it does

For each lead, the tool:

1. Fetches the website from `Website` or `Domain`
2. Extracts useful public signals from the homepage and an about/team/company page when it finds one
3. Looks for LinkedIn links on the site
4. Optionally performs a best-effort public LinkedIn search when no LinkedIn link is found on the site
5. Sends the lead data plus the public research to the OpenAI API
6. Writes an enriched CSV with:
   - `Company Size`
   - `LinkedIn URL`
   - `Reasoning`
   - `Icebreaker 1`
   - `Icebreaker 2`
   - `Confidence`

## Setup

For the CLI only, Python's standard library is enough.

For the API server, install dependencies:

```bash
pip3 install -r requirements.txt
```

Set your environment variables:

```bash
export OPENAI_API_KEY="your_openai_api_key"
export ICEBREAKER_API_TOKEN="choose_a_long_random_token"
```

Optional:

```bash
export OPENAI_MODEL="gpt-4.1-mini"
```

## Run it

```bash
python3 lead_icebreakers.py sample_leads.csv -o leads_with_icebreakers.csv --search-linkedin --debug
```

## Run it as an API

Start the server:

```bash
python3 -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Send one lead row:

```bash
curl -X POST http://127.0.0.1:8000/icebreaker \
  -H "Authorization: Bearer $ICEBREAKER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "Business Name": "Mango Logistics Group",
    "Niche": "Logistics service",
    "City": "London",
    "Website": "https://mangologisticsgroup.co.uk/",
    "Domain": "mangologisticsgroup.co.uk",
    "Name": "David Saunders",
    "First Name": "David",
    "Surname": "Saunders",
    "Email": "dave@example.com"
  }'
```

The response shape is:

```json
{
  "company_size": "11-50",
  "linkedin_url": "https://www.linkedin.com/company/...",
  "reasoning": "Short explanation of why the angle fits an automation agency.",
  "icebreaker_1": "First opener",
  "icebreaker_2": "Second opener",
  "confidence": "high"
}
```

## Structured errors

The API returns structured JSON errors like this:

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Missing bearer token",
    "request_id": "ad11fdb5-fb35-4273-a82c-f6e32de99b44",
    "details": null
  }
}
```

Common status codes:

- `401` for missing or invalid bearer token
- `422` for invalid request body
- `500` for missing server configuration
- `502` for generation or upstream fetch failures

## Docker

Build the image:

```bash
docker build -t lead-icebreaker-api .
```

Run it:

```bash
docker run --rm -p 8000:8000 \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e ICEBREAKER_API_TOKEN="$ICEBREAKER_API_TOKEN" \
  lead-icebreaker-api
```

Then call:

```bash
curl -X POST http://127.0.0.1:8000/icebreaker \
  -H "Authorization: Bearer $ICEBREAKER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"Business Name":"Test Co","Website":"https://example.com"}'
```

## Notes

- LinkedIn is handled on a best-effort basis. The script prefers LinkedIn URLs found directly on the company's website. The public search fallback is less reliable and may break if search engine markup changes.
- The prompt is designed to avoid fake specificity, but model output should still be spot-checked before using it at scale.
- If a row fails, the script keeps going and writes the failure into the output CSV instead of stopping the whole run.
