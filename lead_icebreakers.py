#!/usr/bin/env python3
"""
Generate tailored cold outreach icebreakers from a leads CSV.

The tool reads a CSV with these headers:
Business Name, Niche, City, Website, Domain, Name, First Name, Surname, Email

For each row it:
- fetches the company website
- extracts basic public website signals
- finds LinkedIn URLs on the site when present
- optionally performs a best-effort public search for a LinkedIn profile/company page
- asks the OpenAI API to generate tailored icebreakers

Output is written as a new CSV with the original columns plus enrichment fields.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional, Sequence, Tuple


EXPECTED_HEADERS = [
    "Business Name",
    "Niche",
    "City",
    "Website",
    "Domain",
    "Name",
    "First Name",
    "Surname",
    "Email",
]

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def debug(enabled: bool, message: str) -> None:
    if enabled:
        print(message, file=sys.stderr)


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def looks_like_linkedin(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return "linkedin.com" in parsed.netloc.lower()


def normalize_company_size(value: str) -> str:
    value = clean_text(value).lower()
    if not value:
        return "unknown"
    patterns = [
        (r"\b1\s*[-–]\s*10\b", "1-10"),
        (r"\b11\s*[-–]\s*50\b", "11-50"),
        (r"\b51\s*[-–]\s*200\b", "51-200"),
        (r"\b201\s*[-–]\s*500\b", "201-500"),
        (r"\b500\+\b", "500+"),
        (r"\b501\s*[-–]\s*1000\b", "500+"),
        (r"\b1001\s*[-–]\s*5000\b", "500+"),
        (r"\b5001\s*[-–]\s*10,?000\b", "500+"),
        (r"\b10,?001\+\b", "500+"),
    ]
    for pattern, normalized in patterns:
        if re.search(pattern, value):
            return normalized
    return "unknown"


def extract_company_size_hint(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    explicit_patterns = [
        r"\b1\s*[-–]\s*10\s+employees\b",
        r"\b11\s*[-–]\s*50\s+employees\b",
        r"\b51\s*[-–]\s*200\s+employees\b",
        r"\b201\s*[-–]\s*500\s+employees\b",
        r"\b500\+\s+employees\b",
        r"\b501\s*[-–]\s*1000\s+employees\b",
        r"\b1001\s*[-–]\s*5000\s+employees\b",
        r"\b5001\s*[-–]\s*10,?000\s+employees\b",
        r"\b10,?001\+\s+employees\b",
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_company_size(match.group(0))
    rough_patterns = [
        r"\bteam of (\d{1,5})\b",
        r"\b(\d{1,5}) employees\b",
        r"\b(\d{1,5}) staff\b",
        r"\b(\d{1,5}) people\b",
    ]
    for pattern in rough_patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        try:
            count = int(match.group(1))
        except ValueError:
            continue
        if count <= 10:
            return "1-10"
        if count <= 50:
            return "11-50"
        if count <= 200:
            return "51-200"
        if count <= 500:
            return "201-500"
        return "500+"
    return ""


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value


def first_non_empty(*values: str) -> str:
    for value in values:
        if (value or "").strip():
            return value.strip()
    return ""


@dataclass
class WebsiteResearch:
    homepage_url: str = ""
    title: str = ""
    meta_description: str = ""
    headings: str = ""
    body_snippet: str = ""
    about_page_url: str = ""
    about_snippet: str = ""
    linkedin_urls: Tuple[str, ...] = ()
    company_size_hint: str = ""

    def summary_for_prompt(self) -> str:
        sections = [
            ("Homepage", self.homepage_url),
            ("Title", self.title),
            ("Meta Description", self.meta_description),
            ("Headings", self.headings),
            ("Homepage Snippet", self.body_snippet),
            ("About Page", self.about_page_url),
            ("About Snippet", self.about_snippet),
            ("LinkedIn URLs", ", ".join(self.linkedin_urls)),
        ]
        lines = []
        for label, value in sections:
            if value:
                lines.append(f"{label}: {value}")
        return "\n".join(lines)


class SimpleHTMLExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.meta_description = ""
        self._current_tag = ""
        self._capture_heading = False
        self.headings: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self.text_chunks: List[str] = []
        self._link_href = ""
        self._link_text_chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._current_tag = tag.lower()
        attr_map = dict(attrs)
        if self._current_tag == "meta":
            name = (attr_map.get("name") or "").lower()
            prop = (attr_map.get("property") or "").lower()
            if name == "description" or prop == "og:description":
                content = clean_text(attr_map.get("content") or "")
                if content and not self.meta_description:
                    self.meta_description = content
        if self._current_tag in {"h1", "h2"}:
            self._capture_heading = True
        if self._current_tag == "a":
            self._link_href = attr_map.get("href") or ""
            self._link_text_chunks = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"h1", "h2"}:
            self._capture_heading = False
        if tag == "a":
            link_text = clean_text(" ".join(self._link_text_chunks))
            self.links.append((self._link_href, link_text))
            self._link_href = ""
            self._link_text_chunks = []
        self._current_tag = ""

    def handle_data(self, data: str) -> None:
        text = clean_text(data)
        if not text:
            return
        if self._current_tag == "title" and not self.title:
            self.title = text
        if self._capture_heading:
            self.headings.append(text)
        if self._link_href:
            self._link_text_chunks.append(text)
        self.text_chunks.append(text)


def fetch_url(url: str, timeout: float, debug_logs: bool) -> Tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    debug(debug_logs, f"Fetching: {url}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read()
        return response.geturl(), body.decode(charset, errors="replace")


def trim_snippet(chunks: Sequence[str], max_chars: int = 500) -> str:
    joined = " ".join(chunk for chunk in chunks if chunk)
    joined = clean_text(joined)
    if len(joined) <= max_chars:
        return joined
    return joined[: max_chars - 3].rstrip() + "..."


def extract_site_data(base_url: str, html_text: str) -> WebsiteResearch:
    parser = SimpleHTMLExtractor()
    parser.feed(html_text)
    absolute_links: List[Tuple[str, str]] = []
    linkedin_urls: List[str] = []
    about_page_url = ""
    for href, text in parser.links:
        if not href:
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        absolute_links.append((absolute, text))
        if looks_like_linkedin(absolute) and absolute not in linkedin_urls:
            linkedin_urls.append(absolute)
        lowered = absolute.lower()
        if not about_page_url and any(token in lowered for token in ("/about", "/company", "/team", "/story")):
            about_page_url = absolute
    headings = trim_snippet(parser.headings, max_chars=250)
    body_snippet = trim_snippet(parser.text_chunks, max_chars=550)
    return WebsiteResearch(
        homepage_url=base_url,
        title=parser.title,
        meta_description=parser.meta_description,
        headings=headings,
        body_snippet=body_snippet,
        about_page_url=about_page_url,
        linkedin_urls=tuple(linkedin_urls[:5]),
    )


def find_best_website(row: Dict[str, str]) -> str:
    website = normalize_url(row.get("Website", ""))
    domain = normalize_url(row.get("Domain", ""))
    return first_non_empty(website, domain)


def search_public_linkedin(row: Dict[str, str], timeout: float, debug_logs: bool) -> str:
    search_terms = [
        first_non_empty(row.get("Name", ""), " ".join(filter(None, [row.get("First Name", ""), row.get("Surname", "")]))).strip(),
        row.get("Business Name", "").strip(),
        row.get("City", "").strip(),
    ]
    query = " ".join(term for term in search_terms if term)
    if not query:
        return ""
    full_query = f"site:linkedin.com/in OR site:linkedin.com/company {query}"
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": full_query})
    try:
        _, page = fetch_url(url, timeout=timeout, debug_logs=debug_logs)
    except Exception as exc:  # noqa: BLE001
        debug(debug_logs, f"LinkedIn search failed: {exc}")
        return ""
    matches = re.findall(r'href="(//duckduckgo\.com/l/\?uddg=[^"]+)"', page)
    for raw in matches:
        parsed = urllib.parse.urlparse("https:" + raw)
        params = urllib.parse.parse_qs(parsed.query)
        candidate = urllib.parse.unquote(params.get("uddg", [""])[0])
        if looks_like_linkedin(candidate):
            return candidate
    direct_matches = re.findall(r'https?://[^\s"]+linkedin\.com/[^\s"&]+', page)
    for candidate in direct_matches:
        if looks_like_linkedin(candidate):
            return candidate
    return ""


def search_company_size_hint(row: Dict[str, str], timeout: float, debug_logs: bool) -> str:
    search_terms = [
        row.get("Business Name", "").strip(),
        row.get("Domain", "").strip(),
        row.get("City", "").strip(),
    ]
    query = " ".join(term for term in search_terms if term)
    if not query:
        return ""
    full_query = f'{query} ("employees" OR "team of" OR "staff")'
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": full_query})
    try:
        _, page = fetch_url(url, timeout=timeout, debug_logs=debug_logs)
    except Exception as exc:  # noqa: BLE001
        debug(debug_logs, f"Company size search failed: {exc}")
        return ""
    return extract_company_size_hint(page)


def enrich_website(row: Dict[str, str], timeout: float, debug_logs: bool) -> WebsiteResearch:
    website = find_best_website(row)
    if not website:
        return WebsiteResearch()
    try:
        final_url, html_text = fetch_url(website, timeout=timeout, debug_logs=debug_logs)
        research = extract_site_data(final_url, html_text)
        research.company_size_hint = extract_company_size_hint(html_text)
    except Exception as exc:  # noqa: BLE001
        debug(debug_logs, f"Website fetch failed for {website}: {exc}")
        return WebsiteResearch(homepage_url=website)

    if research.about_page_url and research.about_page_url != research.homepage_url:
        try:
            _, about_html = fetch_url(research.about_page_url, timeout=timeout, debug_logs=debug_logs)
            about_data = extract_site_data(research.about_page_url, about_html)
            research.about_snippet = about_data.body_snippet
            if not research.company_size_hint:
                research.company_size_hint = extract_company_size_hint(about_html)
            merged = list(research.linkedin_urls)
            for url in about_data.linkedin_urls:
                if url not in merged:
                    merged.append(url)
            research.linkedin_urls = tuple(merged[:5])
        except Exception as exc:  # noqa: BLE001
            debug(debug_logs, f"About page fetch failed for {research.about_page_url}: {exc}")

    return research


def build_prompt(row: Dict[str, str], research: WebsiteResearch, linkedin_url: str) -> str:
    contact_name = first_non_empty(
        row.get("Name", ""),
        " ".join(filter(None, [row.get("First Name", ""), row.get("Surname", "")])),
    )
    prompt = f"""
You create sharp, believable cold outreach icebreakers for cold email outreach.

Lead data:
- Business Name: {row.get("Business Name", "")}
- Niche: {row.get("Niche", "")}
- City: {row.get("City", "")}
- Website: {row.get("Website", "")}
- Domain: {row.get("Domain", "")}
- Name: {contact_name}
- Email: {row.get("Email", "")}
- Best LinkedIn URL found: {linkedin_url}
- Explicit company size hint found from public data: {research.company_size_hint or "none"}

Public research:
{research.summary_for_prompt() or "No useful public research found."}

Return valid JSON with this exact shape:
{{
  "company_size": "one of: 1-10, 11-50, 51-200, 201-500, 500+, unknown",
  "reasoning": "one short sentence explaining the personalization logic",
  "icebreakers": [
    "short icebreaker 1",
    "short icebreaker 2"
  ],
  "confidence": "high|medium|low",
  "linkedin_url": "best LinkedIn URL if one is clearly supported, otherwise empty string"
}}

Rules:
- Keep each icebreaker under 30 words.
- Sound specific, natural, light, and observant, not creepy.
- Write them like a sharp human sender who has clearly done a bit of research, not a copywriter or SDR template.
- Do not use questions.
- Do not end with a question mark.
- Avoid hype, praise-heavy language, exclamation marks, and generic compliments.
- Avoid phrases that sound AI-generated or salesy, such as "I noticed", "impressed by", "really stands out", "must be", "curious", "congrats", or "how are you".
- Focus on surface-level but specific signals that show real research:
  - hiring activity
  - messaging on the homepage
  - product or service positioning
  - case studies, testimonials, or client logos
  - team, founder, or company updates
  - the way a service is explained
  - a niche angle or market focus
- The goal is to earn attention and feel relevant, not to pitch automation in the opener.
- Lean more personal and commercial, less operational and systems-focused.
- Do not invent precise facts that are not supported by the research.
- Prefer website-based observations over vague compliments.
- If evidence is thin, be honest and keep the language more general.
- Keep the reasoning concise and practical.
- The reasoning should explain why the line feels personally relevant and likely to get attention.
- Estimate company size conservatively from public signals when possible, otherwise return "unknown".
- Prefer plainspoken wording over polished wording.
- Vary sentence rhythm so the lines do not feel templated.
- Do not default to the words "streamline", "optimize", "operational challenges", "coordination", or "efficiency".
- Good style examples:
  - "Looks like you're hiring SDRs right now—usually a sign outbound is about to ramp hard."
  - "Came across your landing page for [product]—the way you explain [specific feature] is one of the clearest I've seen."
  - "Saw the way you position [service] for [niche]—felt more thought-through than the usual generic angle."
- Bad style examples:
  - "Your operations likely involve complex coordination that could benefit from automation."
  - "You appear to have several workflows that may present efficiency opportunities."
  - "Managing this probably creates a lot of manual work behind the scenes."
""".strip()
    return prompt


def call_openai(prompt: str, model: str, api_key: str, timeout: float) -> Dict[str, object]:
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful sales research assistant that outputs valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }
    request = urllib.request.Request(
        OPENAI_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    return json.loads(content)


def validate_headers(fieldnames: Optional[Sequence[str]]) -> None:
    if not fieldnames:
        raise ValueError("Input CSV appears to be empty.")
    missing = [header for header in EXPECTED_HEADERS if header not in fieldnames]
    if missing:
        raise ValueError("Missing required headers: " + ", ".join(missing))


def read_rows(input_path: str) -> List[Dict[str, str]]:
    with open(input_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        validate_headers(reader.fieldnames)
        return list(reader)


def write_rows(output_path: str, rows: List[Dict[str, str]], fieldnames: Sequence[str]) -> None:
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def enrich_row(
    row: Dict[str, str],
    model: str,
    api_key: str,
    timeout: float,
    delay_seconds: float,
    search_linkedin: bool,
    debug_logs: bool,
) -> Dict[str, str]:
    research = enrich_website(row, timeout=timeout, debug_logs=debug_logs)
    linkedin_url = ""
    if research.linkedin_urls:
        linkedin_url = research.linkedin_urls[0]
    elif search_linkedin:
        linkedin_url = search_public_linkedin(row, timeout=timeout, debug_logs=debug_logs)

    prompt = build_prompt(row, research, linkedin_url)
    result = call_openai(prompt, model=model, api_key=api_key, timeout=timeout)
    icebreakers = result.get("icebreakers") or []
    if delay_seconds > 0:
        time.sleep(delay_seconds + random.uniform(0, 0.5))
    final_linkedin_url = str(result.get("linkedin_url", "")).strip() or linkedin_url
    company_size = research.company_size_hint
    if not company_size:
        company_size = search_company_size_hint(row, timeout=timeout, debug_logs=debug_logs)
    if not company_size:
        company_size = str(result.get("company_size", "unknown") or "unknown")
    generated = {
        "Company Size": company_size,
        "LinkedIn URL": final_linkedin_url,
        "Reasoning": str(result.get("reasoning", "")),
        "Icebreaker 1": str(icebreakers[0] if len(icebreakers) > 0 else ""),
        "Icebreaker 2": str(icebreakers[1] if len(icebreakers) > 1 else ""),
        "Confidence": str(result.get("confidence", "")),
    }
    enriched = dict(row)
    enriched.update(generated)
    return enriched


def generate_single_lead(
    row: Dict[str, str],
    model: str,
    api_key: str,
    timeout: float = 20.0,
    search_linkedin: bool = True,
    debug_logs: bool = False,
) -> Dict[str, str]:
    enriched = enrich_row(
        row=row,
        model=model,
        api_key=api_key,
        timeout=timeout,
        delay_seconds=0.0,
        search_linkedin=search_linkedin,
        debug_logs=debug_logs,
    )
    return {
        "company_size": enriched.get("Company Size", "unknown"),
        "linkedin_url": enriched.get("LinkedIn URL", ""),
        "reasoning": enriched.get("Reasoning", ""),
        "icebreaker_1": enriched.get("Icebreaker 1", ""),
        "icebreaker_2": enriched.get("Icebreaker 2", ""),
        "confidence": enriched.get("Confidence", ""),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate tailored cold outreach icebreakers from a leads CSV.")
    parser.add_argument("input_csv", help="Path to the input CSV.")
    parser.add_argument(
        "-o",
        "--output",
        default="leads_with_icebreakers.csv",
        help="Path to the output CSV.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model name. Defaults to {DEFAULT_MODEL}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds for website fetches and API calls.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=1.0,
        help="Delay between processed rows to be polite to remote sites.",
    )
    parser.add_argument(
        "--search-linkedin",
        action="store_true",
        help="Try a best-effort public search for a LinkedIn URL when none is found on the site.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print progress logs to stderr.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is required.", file=sys.stderr)
        return 1

    try:
        rows = read_rows(args.input_csv)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to read CSV: {exc}", file=sys.stderr)
        return 1

    output_rows: List[Dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        try:
            debug(args.debug, f"[{index}/{len(rows)}] Processing {row.get('Business Name', '').strip() or 'lead'}")
            output_rows.append(
                enrich_row(
                    row=row,
                    model=args.model,
                    api_key=api_key,
                    timeout=args.timeout,
                    delay_seconds=args.delay_seconds,
                    search_linkedin=args.search_linkedin,
                    debug_logs=args.debug,
                )
            )
        except urllib.error.HTTPError as exc:
            failure = dict(row)
            failure["Reasoning"] = f"Failed: HTTP {exc.code}"
            output_rows.append(failure)
        except Exception as exc:  # noqa: BLE001
            failure = dict(row)
            failure["Reasoning"] = f"Failed: {exc}"
            output_rows.append(failure)

    output_fields = list(EXPECTED_HEADERS) + [
        "Company Size",
        "LinkedIn URL",
        "Reasoning",
        "Icebreaker 1",
        "Icebreaker 2",
        "Confidence",
    ]
    write_rows(args.output, output_rows, output_fields)
    print(f"Wrote {len(output_rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
