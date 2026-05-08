# cmvijay.ai

Independent civic tracker for **Tamil Nadu's 2026 government formation**. Tracks the live status of TVK's path to power, the coalition arithmetic, the timeline of events, Vijay's career-to-CM journey, and TVK's "Nine Guarantees" manifesto promise scorecard.

🌐 **Live:** https://cmvijay.ai

---

## What's in this site

- **Hero** — Live status: "Is Vijay CM yet?" with IST clock and days-since-polling counter
- **§01 Coalition Math** — Confirmed (TVK + INC = 113) vs in-talks (CPI/CPI(M)/IUML/VCK = 8) vs opposition vs the 118 majority threshold
- **§02 Timeline** — Apr 23 polling → May 4 results → May 5 Stalin resigns → May 8 Governor rejects claim
- **§03 The Journey** — Vijay's arc: 1984 *Vetri* → 2024 TVK founded → 2024 GOAT → 2026 stakes claim
- **§04 Promise Scorecard** — 73 promises across TVK's Nine Guarantees, sourced and ready to flip from "Pending" to live status on oath day
- **§05 Sources** — 19 numbered, verifiable citations including tvkvijay.com (semi-official), vijay.com (fan-affiliated), ThePrint, Times of India, FilmiBeat, Daily Pioneer, The Logical Indian, OneIndia, The Quint, Wikipedia, voterlist.co.in
- **Bilingual** — English / தமிழ் toggle in header

## Tech stack

- React 18 + Vite
- Tailwind CSS 3 (utility classes only — Tailwind core works in CDN-friendly mode)
- lucide-react (icons)
- Google Fonts: Archivo Black, DM Serif Display, IBM Plex Mono/Sans, Noto Sans Tamil
- Vercel (hosting)
- No backend, no database — pure static site

## Local development

```bash
git clone https://github.com/<you>/cmvijay
cd cmvijay
npm install
npm run dev
```

Open http://localhost:5173

## Updating the site as news evolves

Three constants near the top of `src/App.jsx` control the live data:

```js
// Hero status: flip as the situation moves
const CURRENT_STATUS = "no";   // → "soon" when coalition secured, "yes" when sworn in

// Coalition arithmetic — move parties between buckets as they commit/withdraw
const CONFIRMED = [
  { party: "TVK", seats: 108, ... },
  { party: "INC", seats: 5, ... },
];
const IN_TALKS = [ /* CPI, CPI(M), VCK, IUML */ ];
```

For the **Promise Scorecard**, when a TVK government takes oath, edit each row's status:

```js
{ id: "g1_2500", category: "01 · Women's Welfare", text: "...", status: "pending", ... }
//                                                              ^ change to:
//   "in_progress" | "delivered" | "partial" | "broken"
```

Commit, push, Vercel rebuilds in ~30 seconds.

## Sourcing discipline

Every claim on this site carries a `[n]` superscript citation linking to the **§05 Sources** section, where each source has a publisher, date, and "Verify" external link. When sources conflict, the most recent reliable update wins. Spot an error? File an issue or email corrections@cmvijay.ai.

The TVK manifesto (semi-official, hosted at tvkvijay.com as a 96-page image-rendered PDF) is source [10]. The richest English transcription of that manifesto, organized as Nine Guarantee chapters, is on the fan-affiliated vijay.com — source [11]. Together they form the spine; secondary outlets corroborate.

## Disclaimer

cmvijay.ai is an **independent, non-partisan civic tracker**. Not affiliated with Tamilaga Vettri Kazhagam, Joseph Vijay, or any political party or government body. Data is sourced from the Election Commission of India and public news reporting.

## License

Code: MIT. Content (manifesto promises, timeline events) is public-record reporting and quoted from third-party sources cited inline.
