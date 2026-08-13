"""
feeds.py — RSS feeds + keyword filtering for the Watcher.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Feed:
    outlet: str
    domain: str
    url: str
    tn_only: bool
    notes: str = ""


PHASE_1_FEEDS: list[Feed] = [
    Feed("The Hindu — Tamil Nadu", "thehindu.com",
         "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss",
         True, "Highest signal-to-noise."),
    Feed("ANI — National", "aninews.in",
         "https://aninews.in/rss/category/india.xml",
         False, "National feed; TN needs keyword filter."),
    Feed("ThePrint — India", "theprint.in",
         "https://theprint.in/category/india/feed/",
         False, "Strong on cabinet/governance."),
    Feed("The News Minute — Tamil Nadu", "thenewsminute.com",
         "https://www.thenewsminute.com/feed/tamil-nadu",
         True, "TN-focused; catches editorial detail."),
    Feed("The Federal — Tamil Nadu", "thefederal.com",
         "https://thefederal.com/category/states/south/tamil-nadu/feed/",
         True, "TN-focused."),
]

TVK_KEYWORDS = [
    "vijay", "tvk", "tamilaga vettri kazhagam", "joseph vijay",
    "tamil nadu cabinet", "tamil nadu chief minister", "tamil nadu cm",
    "tamil nadu government", "tamil nadu assembly",
    "aadhav arjuna", "sengottaiyan", "marie wilson", "nirmalkumar",
    "tasmac", "singappen", "vetri thamizhagam", "vetri tamizhagam",
    "kmut", "madhippumigu magalir",
    "tamizhagam",
]


def is_candidate_relevant(title: str, summary: str = "") -> bool:
    blob = f"{title} {summary}".lower()
    if not any(kw in blob for kw in TVK_KEYWORDS):
        return False

    # Cinema-Vijay heuristic: skip if film keywords dominate
    has_govt_term = any(kw in blob for kw in (
        "chief minister", "cm ", "cabinet", "minister", "assembly",
        "government", "govt", "tvk", "tamilaga vettri", "budget",
        "policy", "scheme", "order", "law", "portfolio",
    ))
    has_film_term = any(kw in blob for kw in (
        "film", "movie", "trailer", "box office", "screening",
    ))
    if has_film_term and not has_govt_term:
        return False

    return True
