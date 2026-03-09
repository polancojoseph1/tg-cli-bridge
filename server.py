import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

import health
from config import (
    ALLOWED_USER_ID, ALLOWED_USER_IDS, USER_NAMES, HOST, PORT, VOICE_MAX_LENGTH, WEBHOOK_URL,
    CLI_RUNNER, BOT_NAME, MEMORY_DIR, RESUME_PATH,
    USER_FULL_NAME, USER_BACKGROUND, USER_TRUE_FACTS, EMAIL_SIGNATURE,
    is_cli_available, validate_config, logger,
)
from runners import create_runner
from telegram_handler import send_message, send_voice, send_photo, send_video, send_chat_action, download_photo, download_document, register_webhook, close_client
from image_handler import generate_image
from voice_handler import download_voice, transcribe_audio, text_to_speech, cleanup_file
import memory_handler
import task_handler
import daily_report
from instance_manager import InstanceManager, Instance
import router
import agent_manager
from agent_registry import create_agent, resolve_agent, list_agents, update_agent, delete_agent, get_agent
from agent_skills import SKILL_PACKS, list_skills

# Optional modules (graceful degradation if not present)
try:
    import screen_recorder
except ImportError:
    screen_recorder = None  # type: ignore

try:
    import scheduler
except ImportError:
    scheduler = None  # type: ignore

try:
    import proactive_worker
except ImportError:
    proactive_worker = None  # type: ignore

try:
    import task_orchestrator
except ImportError:
    task_orchestrator = None  # type: ignore

try:
    import research_handler
except ImportError:
    research_handler = None  # type: ignore

# Initialize the CLI runner
runner = create_runner()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# -- Jobs DB & resume --------------------------------------------------------

DB_PATH = str(Path(MEMORY_DIR) / "jobs.db")
RESUME_PATH = os.environ.get("RESUME_PATH", "")

# -- Standalone prompt strings (avoids escape issues inside PROMPTS dict) --------

_PROMPT_LINKEDIN_SCRAPER = """You are a Job Scraper Agent. Search LinkedIn Jobs for relevant software engineering openings and save new ones to the local jobs database.

CANDIDATE PROFILE:
(Loaded from your USER.md / MEMORY.md files at runtime. Configure your target titles, locations, and tech stack there.)

STEP 1 - SEARCH:
Run ALL of these web searches:
  1. site:linkedin.com/jobs software engineer remote
  2. site:linkedin.com/jobs full stack engineer remote
  3. site:linkedin.com/jobs AI engineer OR ML engineer remote
  4. site:linkedin.com/jobs software engineer New York
  5. site:linkedin.com/jobs software engineer Poland OR Warsaw OR Krakow
  6. site:linkedin.com/jobs backend engineer remote

For each result extract: title, company, location, job_url, tech_stack (from description), posted_date.
Source: if location contains Poland/Warsaw/Krakow -> 'linkedin-pl', else -> 'linkedin-us'.

STEP 2 - SAVE:
POST each job to http://localhost:8585/jobs with this JSON body:
{"job_id": "<company-title-date slugified e.g. stripe-software-engineer-2026-03-01>", "title": "...", "company": "...", "job_url": "...", "location": "...", "tech_stack": "...", "posted_date": "...", "source": "linkedin-us or linkedin-pl", "status": "New"}

Duplicates are silently ignored by the DB (UNIQUE constraint on company+title).

STEP 3 - RETURN JSON SUMMARY ONLY (no other text):
{"new_jobs_saved": <n>, "duplicates_skipped": <n>, "errors": <n>, "jobs": [{"title": "...", "company": "...", "location": "..."}]}

RULES:
  - Only save engineering roles (software/AI/backend/fullstack)
  - Skip roles requiring 10+ years experience or clearly principal/staff level
  - Skip non-engineering roles (product manager, designer, sales, etc.)
  - Process 20-50 jobs per run
  - Return ONLY the JSON summary, no other text"""


_PROMPT_HIRING_MANAGER_RESEARCH = """You are a Hiring Contact Researcher. Find the real hiring manager or recruiter for this role. Use web search. Do not guess or fabricate.

JOB:
  Company: {company}
  Domain: {company_domain}
  Role: {title}
  Job URL: {job_url}
  Location: {location}
  Tech stack: {tech_stack}

SEARCH STRATEGY (run ALL of these):
  1. site:linkedin.com/in "{company}" engineering manager OR recruiter OR "head of engineering"
  2. site:linkedin.com/in "{company}" "talent acquisition" OR "technical recruiter"
  3. "{company}" site:{company_domain} team about leadership
  4. "{company} engineering manager" OR "{company} CTO" OR "{company} VP engineering"
  5. Visit {job_url} directly - sometimes names the hiring manager

PRIORITY ORDER: hiring manager > technical recruiter > Head of Eng/CTO > HR

RULES:
  - Do NOT fabricate emails. Only report emails you actually found published online.
  - Do NOT use careers@/jobs@/hr@ as primary email (alternative_emails only).
  - A name + title + LinkedIn URL with no email is still a valuable result.
  - Aim for 3-5 contacts in all_contacts.

Return ONLY valid JSON, no other text:
{
  "email": "verified published email or empty string",
  "name": "best contact full name",
  "person_title": "their job title",
  "linkedin_url": "their LinkedIn URL if found",
  "confidence": "high|medium|low",
  "all_contacts": [{"name": "...", "title": "...", "email": "...", "linkedin_url": "...", "source": "..."}],
  "alternative_emails": ["careers@domain"],
  "source_urls": ["..."],
  "reasoning": "brief summary of what was found"
}"""


_PROMPT_EMAIL_DRAFT_GENERIC = (
    "You are running one cold email drafting pipeline for a generic company inbox "
    "(no named contact). Complete all 3 steps and return JSON. "
    "Do not use any tools — pure text reasoning only.\n\n"

    "JOB CONTEXT:\n"
    "Title: {title}\n"
    "Company: {company}\n"
    "Tech stack: {tech_stack}\n"
    "Location: {location}\n"
    "Region: {region}\n"
    "Company info: {company_info}\n\n"

    f"CANDIDATE BACKGROUND:\n{USER_BACKGROUND}\n\n"

    f"TRUE FACTS (never contradict): {USER_TRUE_FACTS}\n\n"

    "---\n\n"

    "STEP 1 — DRAFT THE EMAIL:\n\n"
    "Rules:\n"
    "- No greeting line — start directly with Sentence 1\n"
    "- No subject line, no sign-off\n"
    "- Do NOT address a person by name. Do NOT write 'Hi team' or any greeting.\n"
    "- Under 120 words total\n"
    "- No dashes of any kind (em dash, en dash, hyphen as dash — zero)\n"
    "- No filler phrases: 'I am excited', 'I would be a great fit', 'I am passionate', "
    "'I look forward to', 'I would love to'\n"
    "- Each sentence is its own paragraph separated by a blank line\n"
    "- Sound like a real person reaching out, not a template\n\n"

    "Structure (4 sentences, exact order):\n"
    f"Sentence 1 (fixed formula): \"My name is {USER_FULL_NAME}, a software engineer "
    "writing about the {title} opening at {company}.\"\n"
    "Sentence 2: Why the candidate specifically wants to work at {company}. "
    "Must reference something real and specific to THIS company — not a statement that fits any "
    "company in the same space. Convey clear intention to be there, not just admiration from the outside.\n"
    "Sentence 3: The single most relevant piece of the candidate's experience for this role. "
    "Name the actual thing built or shipped — not a skill category.\n"
    "Sentence 4: Ask if someone on the team has time for a quick call.\n\n"

    "---\n\n"

    "STEP 2 — SELF-TEST (max 3 rounds):\n"
    "Check your draft against all rules:\n"
    "1. No subject line\n"
    "2. First line is Sentence 1 — no greeting before it\n"
    "3. No sign-off\n"
    "4. Under 120 words\n"
    "5. Zero dashes\n"
    "6. None of the banned filler phrases\n"
    f"7. Sentence 1 starts with: 'My name is {USER_FULL_NAME}' and mentions the role + company\n"
    "8. Sentence 2 is specific to {company} (not a generic industry statement) and expresses "
    "clear intention to work there\n"
    "9. Sentence 3 names an actual project, system, or deliverable — not a skill category\n"
    "10. Sentence 4 is a CTA asking for a call\n"
    "11. Exactly 4 sentences total\n"
    "12. Each sentence is its own paragraph\n"
    "13. No person name, no greeting anywhere in the email\n"
    "If any fail, revise and recheck. Max 3 rounds total.\n\n"

    "---\n\n"

    "STEP 3 — AI DETECTION + FACT CHECK:\n"
    "Check for AI writing tells: overly smooth structure, assembled phrases, abstract framing, "
    "transitions too clean, phrases no human would say out loud, hollow impressive sentences.\n"
    "Verify all facts against TRUE FACTS listed above.\n"
    "If AI writing detected OR facts wrong: rewrite to fix. Keep same 4-sentence structure, "
    "under 120 words, zero dashes, no filler.\n\n"

    "Return ONLY valid JSON, no other text, no markdown:\n"
    '{{"email": "<final complete email starting with Sentence 1>", '
    '"ai_detected": true or false, "fact_violations": [], "rounds": 1}}'
)


# -- Prompt dictionary -------------------------------------------------------
# All AI prompts live here. Use {placeholders} for dynamic values.
# View all prompts at GET /prompts

PROMPTS = {

    # Perplexity — finds real people (names + titles) at a company who are in hiring positions.
    # Used by POST /agent/email/research. Variables: {company}, {company_domain}, {title},
    # {job_url}, {location}, {tech_stack}
    "email_research": (
        "You are a HIRING CONTACT RESEARCHER. Your mission: find the REAL NAMES and TITLES of people "
        "who are involved in hiring for this role. Do NOT guess emails. Do NOT construct emails. "
        "Find PEOPLE. Names, titles, LinkedIn profiles, and any verified emails you actually find published online.\n\n"
        "Company: {company}\n"
        "Domain: {company_domain}\n"
        "Role: {title}\n"
        "Job URL: {job_url}\n"
        "Location: {location}\n"
        "Tech stack: {tech_stack}\n\n"
        "YOUR GOAL: Find as many real people as possible who are in hiring positions at this company. "
        "I need NAMES. Not inboxes. Not aliases. HUMAN BEINGS with job titles.\n\n"
        "WHO TO LOOK FOR (in priority order):\n"
        "  1. The hiring manager for this specific role (engineering manager, team lead, director)\n"
        "  2. Technical recruiters at this company\n"
        "  3. Head of Engineering / VP Engineering / CTO\n"
        "  4. HR leads or talent acquisition managers\n"
        "  5. Any team members who work on the team this role is for\n\n"
        "SEARCH STRATEGIES (do ALL of these):\n"
        '  1. LinkedIn: "site:linkedin.com/in {company} engineering manager" — find the team lead\n'
        '  2. LinkedIn: "site:linkedin.com/in {company} recruiter" — find recruiters\n'
        '  3. LinkedIn: "site:linkedin.com/in {company} VP engineering OR head of engineering OR CTO"\n'
        '  4. LinkedIn: "site:linkedin.com/in {company} talent acquisition"\n'
        '  5. Company website: "{company_domain}" about page, team page, leadership page\n'
        '  6. GitHub: "{company} org members" — developers often list their role and name\n'
        '  7. Blog/press: "{company} engineering blog author" — blog authors are real employees\n'
        '  8. Job posting: check {job_url} for any contact name mentioned\n'
        '  9. Glassdoor/Blind: "{company} hiring manager" — sometimes names come up in reviews\n'
        '  10. Crunchbase: "{company}" leadership team\n\n'
        "WHAT COUNTS AS A GOOD FIND:\n"
        "  - A full name + job title + where you found them (LinkedIn URL, company page, etc.)\n"
        "  - An actual email you found PUBLISHED online (on their GitHub, blog, company page). Do NOT fabricate these.\n"
        "  - The more people you find, the better. I want a LIST of contacts, not just one.\n\n"
        "WHAT TO NEVER DO:\n"
        "  - Do NOT construct emails from name patterns. If you didn't find the email published, leave it blank.\n"
        "  - Do NOT guess. No firstname.lastname@ guesses. Only report emails you actually found.\n"
        "  - Do NOT put careers@, jobs@, hr@ as the primary email. Those go in alternative_emails only.\n\n"
        "FINAL OUTPUT — return ONLY valid JSON, no extra text:\n"
        "{{\n"
        '  "email": "ONLY if you found a real person email published online, otherwise empty string",\n'
        '  "name": "Full Name of the best contact (hiring manager > recruiter > leadership)",\n'
        '  "person_title": "Their actual job title",\n'
        '  "confidence": "high = found verified email published online | medium = found name and title but no email | low = could not find anyone",\n'
        '  "personal_found": true,\n'
        '  "all_contacts": [\n'
        '    {{"name": "Full Name", "title": "Job Title", "email": "only if found published, else empty string", "source": "URL where you found them"}}\n'
        '  ],\n'
        '  "reasoning": "Brief summary of your research process and what you found",\n'
        '  "alternative_emails": ["generic department emails like careers@{company_domain}"],\n'
        '  "source_urls": ["all URLs you used as sources"]\n'
        "}}\n\n"
        "CRITICAL: The all_contacts array is the MOST IMPORTANT field. Put EVERY person you found in there, "
        "even if you only found their name and title with no email. The more contacts the better. "
        "Aim for at least 3 to 5 contacts per company. Each contact MUST have name, title, and source URL."
    ),

    # Perplexity — researches a known hiring contact to build personalization hooks for cold email.
    # Used by POST /agent/hm/info. Variables: {name}, {title}, {company}, {email}, {linkedin_hint}
    "hm_info": (
        "Research this hiring contact and return everything useful for personalizing a cold outreach email.\n\n"
        "Name: {name}\n"
        "Title: {title}\n"
        "Company: {company}\n"
        "Email: {email}"
        "{linkedin_hint}\n\n"
        "Find and return:\n"
        "1. Their current role and how long they have been there\n"
        "2. Career background (previous companies, notable roles)\n"
        "3. Any recent LinkedIn posts, articles, or public activity\n"
        "4. Their stated interests, passions, or focus areas (from bio, posts, or interviews)\n"
        "5. Any mutual context (e.g. same school, same previous employer as candidate)\n"
        "6. The best angle for a cold email opener that references something real about them\n\n"
        f"Candidate background (for finding mutual context): {USER_BACKGROUND}\n\n"
        "Return ONLY valid JSON:\n"
        "{{\n"
        '  "full_name": "their full name exactly as it appears on LinkedIn or their profile",\n'
        '  "current_role_summary": "1-2 sentences on what they do now",\n'
        '  "career_background": "brief career history",\n'
        '  "recent_activity": "any recent posts, articles, or public statements",\n'
        '  "interests": "their stated focus areas or passions",\n'
        '  "personalization_hook": "the single best opening line hook for a cold email to this person",\n'
        '  "mutual_context": "any shared background with the candidate, or empty string",\n'
        '  "linkedin_url": "confirmed LinkedIn URL if found"\n'
        "}}"
    ),

    # Claude browser automation — visits LinkedIn profiles for contacts already in the employee
    # table and populates their info field with structured personalization data.
    # Run this manually in Telegram once you have a batch of contacts with linkedin_urls.
    # No variables needed — reads directly from the DB via localhost:8585.
    "linkedin_profile": (
        "You are a LinkedIn Profile Researcher. Your job is to visit LinkedIn profiles for hiring "
        "contacts in the employee database and extract personalization data for cold outreach emails.\n\n"
        "SETUP:\n"
        "  - Employee DB API: http://localhost:8585\n"
        "  - GET /employees — returns all contacts\n"
        "  - PATCH /employees — updates a contact's info, full_name, and linkedin_url\n\n"
        "WORKFLOW:\n"
        "1. Call GET http://localhost:8585/employees\n"
        "2. Filter to contacts where linkedin_url is set AND info is empty (or info = \"\")\n"
        "3. For each contact (process up to 20 per run):\n"
        "   a. Navigate to their LinkedIn profile URL in the browser\n"
        "   b. Extract the following from their profile:\n"
        "      - full_name: their EXACT full name as it appears on LinkedIn (e.g. 'David Chen', 'Jane Smith')\n"
        "      - current_role_summary: what they do now and how long they have been there\n"
        "      - career_background: previous companies and notable roles (2-3 sentences)\n"
        "      - recent_activity: any recent posts, shared articles, or visible activity\n"
        "      - interests: stated interests, skills, or focus areas from their bio or about section\n"
        "      - personalization_hook: the single best opening line for a cold email — must reference "
        "something specific and real about them, not a generic compliment\n"
        "      - mutual_context: any overlap with the candidate's background — empty string if none\n"
        "   c. Call PATCH http://localhost:8585/employees with:\n"
        "      {\"email\": \"<their email>\", \"full_name\": \"<their full name>\", "
        "\"info\": <extracted data as JSON object>, "
        "\"linkedin_url\": \"<confirmed profile URL>\"}\n"
        "   d. Confirm the save succeeded before moving to the next contact\n"
        "4. After finishing, report a summary:\n"
        "   - X contacts updated\n"
        "   - Y skipped (private profile or not found)\n"
        "   - Any notable personalization hooks found worth highlighting\n\n"
        "RULES:\n"
        "  - full_name is CRITICAL — always extract and save it. This is used for email greetings.\n"
        "  - Only extract publicly visible info — no login or special access needed\n"
        "  - If a profile is private, unavailable, or returns an error — skip it and note it\n"
        "  - The personalization_hook must be specific. 'I admire your work' is useless.\n"
        "    Good examples: 'I saw your post on agentic workflows last week', "
        "'Noticed you built the ML infra at X before joining Y'\n"
        "  - Store info as a proper JSON object, not a string\n"
        "  - Do not fabricate anything — only report what you actually see on the profile"
    ),

    # Orchestrator — fetches all contacts with empty info from the DB, splits them into 6 batches,
    # and spawns 6 parallel sub-agents to research each batch end-to-end.
    # No variables needed — reads directly from the DB via localhost:8585.
    "hm_info_orchestrator": (
        "You are a Hiring Manager Research Orchestrator. Your job is to research all unprocessed "
        "hiring contacts in the employee database and populate their info fields with personalization "
        "data for cold outreach emails. You will do this by spawning 6 parallel sub-agents, each "
        "working through their own batch of contacts.\n\n"

        "STEP 1 — FETCH THE QUEUE:\n"
        "Call GET http://localhost:8585/employees\n"
        "Filter to contacts where info is null, empty string, or '{}'. These are your queue.\n"
        "If the queue is empty, report '0 contacts to process' and stop.\n\n"

        "STEP 2 — SPLIT INTO BATCHES:\n"
        "Divide the queue into up to 6 roughly equal batches (e.g. 18 contacts = 3 per agent).\n"
        "If there are fewer than 6 contacts, use fewer agents (1 per contact).\n\n"

        "STEP 3 — SPAWN 6 PARALLEL SUB-AGENTS:\n"
        "Launch all batches simultaneously using the Task tool. Each sub-agent receives:\n"
        "  - Their assigned list of contacts (name, title, company, email, linkedin_url)\n"
        "  - Full instructions to research and save each one\n\n"

        "Each sub-agent must follow these instructions for EVERY contact in its batch:\n\n"

        "  RESEARCH PROCESS (per contact):\n"
        "  1. Search for the person online. Use all available sources:\n"
        "     - LinkedIn profile (use linkedin_url if provided, otherwise search 'site:linkedin.com/in {name} {company}')\n"
        "     - Web search: '{name} {title} {company}'\n"
        "     - Twitter/X, personal blog, GitHub, company about page, press mentions\n"
        "     - Recent posts, articles, interviews, or talks\n"
        "  2. Extract the following from what you find:\n"
        "     - full_name: their EXACT full name (e.g. 'David Chen', 'Jane Smith') — CRITICAL for email greetings\n"
        "     - current_role_summary: what they do now and how long they have been there\n"
        "     - career_background: previous companies and notable roles (2-3 sentences)\n"
        "     - recent_activity: any recent posts, articles, or public statements\n"
        "     - interests: stated interests, skills, or focus areas\n"
        "     - personalization_hook: the single best opening line for a cold email — must reference "
        "something specific and real, not a generic compliment. Examples: 'I saw your post on agentic "
        "workflows last week', 'Noticed you built the ML infra at X before joining Y'\n"
        "     - mutual_context: any overlap with the candidate's background — empty string if none\n"
        "     - linkedin_url: confirmed LinkedIn URL\n"
        "  3. Save via PATCH http://localhost:8585/employees:\n"
        "     {\"email\": \"<their email>\", \"full_name\": \"<their full name>\", "
        "\"info\": <extracted data as JSON object>, "
        "\"linkedin_url\": \"<confirmed URL or original if unchanged>\"}\n"
        "  4. Confirm the save succeeded (HTTP 200, ok: true) before moving to the next contact.\n"
        "  5. If a person cannot be found after a genuine search, save info as "
        "{\"error\": \"not found\"} so they are not retried.\n\n"

        "  RULES:\n"
        "  - The personalization_hook must be specific. 'I admire your work' is useless.\n"
        "  - Store info as a proper JSON object, not a string.\n"
        "  - Do not fabricate anything — only report what you actually find.\n"
        "  - Process contacts one at a time within your batch. Do not skip any.\n\n"

        "  At the end, each sub-agent should return a brief summary:\n"
        "  - X contacts researched successfully\n"
        "  - Y contacts not found\n"
        "  - Any standout personalization hooks worth highlighting\n\n"

        "STEP 4 — CONSOLIDATE AND REPORT:\n"
        "After all 6 sub-agents complete, compile a final summary:\n"
        "  - Total contacts processed\n"
        "  - Total saved successfully\n"
        "  - Total not found\n"
        "  - Any notable personalization hooks found worth highlighting\n"
        "  - Any errors or failed saves"
    ),

    "linkedin_scraper": _PROMPT_LINKEDIN_SCRAPER,
    "hiring_manager_research": _PROMPT_HIRING_MANAGER_RESEARCH,
    "email_draft_generic": _PROMPT_EMAIL_DRAFT_GENERIC,

    # Two-agent email drafting system powered by Claude Haiku (run_query / Claude CLI).
    # Used by POST /agent/email/draft. One prompt key, two real agents, code-enforced loop (max 3 rounds).
    # RULE: n8n workflow must stay in sync with this prompt. This is the source of truth.
    "email_draft": {

        # Agent 1 — Drafter. Variables: {title}, {company}, {tech_stack}, {location}, {region},
        # {contact_name}, {first_name}, {greeting}, {mutual_context}, {personalization_hook}, {feedback}
        # Returns: email body only (no greeting, no subject, no sign-off).
        "drafter": (
            "You are a cold email copywriter. A QA agent will review your output against the 14 rules below. "
            "If violations are found you will receive specific feedback and must revise. Maximum 3 rounds total.\n\n"

            "Return ONLY the email body — no greeting line, no subject line, no sign-off.\n"
            "Do NOT ask questions. Do NOT request more information. Work only with what you have been given.\n"
            "Under 130 words total.\n"
            "Each sentence must be its own paragraph, separated by a blank line.\n"
            "ABSOLUTELY NO DASHES. Not em dashes, not hyphens used as dashes, not en dashes. Zero. "
            "Rewrite any sentence that would need one.\n"
            "No filler phrases: 'I am excited', 'I would be a great fit', 'I am passionate', "
            "'I look forward to', 'I would love to'.\n\n"

            "WRITING STYLE — this is what good looks like:\n"
            "- Tight and direct. Every word earns its place. No sentence should be longer than it needs to be.\n"
            "- Sound like a real person writing a real email, not a template being filled in.\n"
            "- No cinematic or assembled phrases. Avoid constructions like 'makes this feel like X', "
            "'not just watching from the outside', 'a dynamic I find fascinating'.\n"
            "- No stock cold email closers like 'to see if there is a fit' or 'I would love to connect'.\n"
            "- Short beats elaborate. If two words work, do not use five.\n"
            "- The best sentence is one someone would actually say out loud.\n\n"

            "RULES (you will be graded on all 14):\n"
            "1. No subject line\n"
            "2. First line is Sentence 1 — do not add any greeting before it\n"
            "3. No sign-off (Best, Thanks...)\n"
            "4. Under 130 words\n"
            "5. Zero dashes of any kind\n"
            "6. None of the filler phrases listed above\n"
            "7. Sentence 1: name + role + company (exact formula below)\n"
            "8. Sentence 2: correct personalization path — PATH A, B, or C (see below)\n"
            "9. Sentence 3: specific to {company} — not a statement that fits any company in the same industry\n"
            "10. Sentence 4: concrete, specific experience — name the actual thing built or shipped, not a category\n"
            "11. Sentence 5: CTA asking for a quick call\n"
            "12. 5 sentences if mutual_context is provided, 4 if mutual_context is empty\n"
            "13. Each sentence is its own paragraph, separated by a blank line\n"
            "14. Sentence 3 must convey clear intention to work at {company} — not just admiration or interest from the outside. "
            "FAIL if sentence 3 only says why the company is impressive without saying why the candidate wants to be there.\n\n"

            "STRUCTURE (exact order):\n\n"

            f"Sentence 1: \"My name is {USER_FULL_NAME}, a software engineer reaching out about the "
            "{title} opening at {company}.\"\n\n"

            "Sentence 2 — CONDITIONAL (pick exactly one path):\n"
            "  PATH A — mutual_context is not empty: write one genuinely enthusiastic sentence about the shared "
            "background or connection. Express real curiosity or admiration for how their path intersects with yours — "
            "not a flat acknowledgment. Show that you actually find it interesting, e.g. 'Moving from X to Y to Z "
            "must have been quite a journey.' This is the top priority.\n"
            "  PATH B — mutual_context is empty AND personalization_hook is not empty: write one sentence expressing "
            "genuine enthusiasm or curiosity about something specific in their story or work — make it clear you find "
            "it genuinely interesting, not just that you noticed it. Not over-the-top, but warm and real.\n"
            "  PATH C — both are empty: skip sentence 2 entirely. Go straight to sentence 3.\n\n"

            "Sentence 3: Why the candidate specifically wants to work at {company}. Two requirements: "
            "(1) reference something real and specific about {company} — not something that fits any company in the same space, "
            "(2) make it clear this is where he wants to be, not just that he finds the company interesting from the outside. "
            "There should be a sense of intention — why THIS company, for THIS reason, is the place he wants to contribute.\n\n"

            "Sentence 4: The single most relevant piece of experience from the candidate background for THIS role. "
            "Name the actual thing built or shipped. Not 'I have experience in X' — be concrete.\n\n"

            "Sentence 5: Ask if they have time for a quick call.\n\n"

            "---\n"
            "Job: {title} at {company}\n"
            "Tech stack: {tech_stack}\n"
            "Location: {location}\n"
            "Region: {region}\n"
            "Contact full name (from DB): {contact_name}\n"
            "Contact first name (use this for any in-body name references): {first_name}\n"
            "mutual_context: {mutual_context}\n"
            "personalization_hook: {personalization_hook}\n\n"

            "Company context (use this to write a specific sentence 3):\n{company_info}\n\n"

            f"Candidate background: {USER_BACKGROUND}\n\n"

            "{feedback}"
        ),

        # Agent 3 — AI Detector + Fact Checker. Variables: {draft_email}
        # Checks for AI-sounding language AND false claims against candidate's true background.
        # Returns: valid JSON — ai_detected, fact_violations, ai_tells, final_email.
        "ai_detector": (
            "You are an AI writing detector and fact checker for cold outreach emails. You have two jobs:\n\n"

            "JOB 1 — DETECT AI WRITING:\n"
            "Look for these AI tells:\n"
            "- Overly smooth or symmetrical sentence structure\n"
            "- Phrases that feel assembled rather than felt (e.g. 'makes this feel less like X and more like Y')\n"
            "- Abstract framing instead of direct personal voice\n"
            "- Transitions that are too clean or too logical\n"
            "- Any phrase a human would not actually say in a real conversation\n"
            "- Sentences that sound impressive but have no texture or specificity\n\n"

            "JOB 2 — FACT CHECK:\n"
            "Verify every factual claim in the email against this candidate's TRUE background. "
            "Flag anything false, misleading, or ambiguous:\n"
            f"TRUE FACTS:\n{USER_TRUE_FACTS}\n"
            "- Do NOT allow ambiguous phrasing that could imply the candidate worked somewhere they did not.\n\n"

            "If AI writing is detected OR facts are wrong: rewrite the email.\n"
            "Keep all true facts, keep 5 paragraphs one sentence each, same order, under 130 words, "
            "zero dashes of any kind, no filler phrases ('I am excited', 'I would be a great fit', "
            "'I am passionate', 'I look forward to', 'I would love to').\n\n"

            "Do NOT use any tools. Output ONLY raw JSON. No markdown. No backticks. No explanation.\n\n"

            "Output format:\n"
            "{{\n"
            '  "ai_detected": true or false,\n'
            '  "fact_violations": ["any false or misleading claims found"],\n'
            '  "ai_tells": ["specific AI-sounding phrases"],\n'
            '  "final_email": "humanized, fact-correct email — or original unchanged if both checks pass"\n'
            "}}\n\n"

            "EMAIL TO REVIEW:\n"
            "{draft_email}"
        ),

        # Agent 2 — QA Tester. Variables: {draft_email}, {mutual_context}, {personalization_hook},
        # {title}, {company}, {round}
        # Returns: valid JSON only — verdict, violations list, corrected_email.
        "tester": (
            "You are a QA agent reviewing a cold outreach email draft. "
            "Check the draft against all 14 rules below. "
            "Return ONLY valid JSON — no extra text, no markdown fences.\n\n"

            "RULES:\n"
            "1. No subject line included\n"
            "2. First line must be Sentence 1 — FAIL if there is a greeting line before it\n"
            "3. No sign-off (Best, Thanks...) included\n"
            "4. Under 130 words total — count carefully\n"
            "5. Zero dashes — no em dash (—), en dash (–), or hyphen used as a sentence separator\n"
            "6. No filler phrases: 'I am excited', 'I would be a great fit', 'I am passionate', "
            "'I look forward to', 'I would love to'\n"
            "7. Sentence 1 introduces sender name + role + target company and title\n"
            "8. Sentence 2 personalization — check which path was taken:\n"
            "   PATH A: mutual_context was provided → sentence 2 must express genuine enthusiasm or curiosity about "
            "the shared background or connection, not just acknowledge it. FAIL if the sentence merely states the "
            "connection without any warmth or genuine interest.\n"
            "   PATH B: mutual_context was empty, personalization_hook was provided → sentence 2 must express "
            "genuine enthusiasm or curiosity about something specific in this person's story or work. FAIL if it "
            "reads as a flat observation with no warmth.\n"
            "   PATH C: both were empty → there must be no sentence 2\n"
            "9. Sentence 3 is specific to {company} — FAIL if the sentence could apply to any company "
            "in the same industry without changing a word\n"
            "10. Sentence 4 names an actual project, system, or deliverable — FAIL if it only names a skill category\n"
            "11. Sentence 5 is a CTA asking for a quick call\n"
            "12. Sentence count: 5 if mutual_context was provided, 4 if mutual_context was empty\n"
            "13. Each sentence is its own paragraph separated by a blank line — FAIL if sentences are run together\n"
            "14. Sentence 3 must convey clear intention to work at {company} — FAIL if it only says why the company "
            "is impressive or interesting without expressing that the candidate wants to contribute there specifically\n\n"

            "CONTEXT:\n"
            "mutual_context: {mutual_context}\n"
            "personalization_hook: {personalization_hook}\n"
            "Role: {title} at {company}\n"
            "Round: {round} of 3\n\n"

            "DRAFT TO REVIEW:\n"
            "{draft_email}\n\n"

            "Return ONLY valid JSON:\n"
            "{{\n"
            '  "verdict": "PASS or FAIL",\n'
            '  "round": {round},\n'
            '  "violations": [\n'
            '    {{"rule": <number>, "description": "what failed and why"}}\n'
            '  ],\n'
            '  "corrected_email": "full corrected body if FAIL, empty string if PASS"\n'
            "}}"
        ),
    },
}


async def _init_db() -> None:
    """Create the jobs table if it doesn't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                company TEXT DEFAULT '',
                job_url TEXT DEFAULT '',
                location TEXT DEFAULT '',
                salary TEXT DEFAULT '',
                tech_stack TEXT DEFAULT '',
                posted_date TEXT DEFAULT '',
                source TEXT DEFAULT '',
                status TEXT DEFAULT 'New',
                hiring_manager_email TEXT DEFAULT '',
                email_sent_date TEXT DEFAULT '',
                company_website TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                fit_score INTEGER DEFAULT 0,
                is_ai_agent_role INTEGER DEFAULT 0,
                seniority TEXT DEFAULT 'mid',
                priority TEXT DEFAULT 'low',
                reason TEXT DEFAULT '',
                alternative_emails TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(company, title)
            )
        """)
        # Migration: add alternative_emails to existing DBs that predate this column
        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN alternative_emails TEXT DEFAULT ''")
        except Exception:
            pass  # Column already exists

        await db.execute("""
            CREATE TABLE IF NOT EXISTS employee (
                email TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                title TEXT DEFAULT '',
                company TEXT DEFAULT '',
                linkedin_url TEXT DEFAULT '',
                info TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS job_employee (
                job_id TEXT NOT NULL,
                email TEXT NOT NULL,
                is_primary INTEGER DEFAULT 0,
                PRIMARY KEY (job_id, email)
            )
        """)
        # Migrations — add columns that may not exist in older DBs
        for migration in [
            "ALTER TABLE employee ADD COLUMN full_name TEXT DEFAULT ''",
            "ALTER TABLE employee ADD COLUMN email_draft TEXT DEFAULT ''",
            "ALTER TABLE employee ADD COLUMN draft_status TEXT DEFAULT 'pending'",
            "ALTER TABLE employee ADD COLUMN audit_status TEXT DEFAULT NULL",
            "ALTER TABLE employee ADD COLUMN audit_notes TEXT DEFAULT NULL",
            "ALTER TABLE employee ADD COLUMN is_generic INTEGER DEFAULT NULL",
            "ALTER TABLE employee ADD COLUMN greeting TEXT DEFAULT ''",
            "ALTER TABLE employee ADD COLUMN approved INTEGER DEFAULT NULL",
        ]:
            try:
                await db.execute(migration)
            except Exception:
                pass  # Column already exists
        # Seed draft_status for pre-existing rows
        await db.execute(
            "UPDATE employee SET draft_status = 'done' WHERE length(email_draft) > 0 AND draft_status = 'pending'"
        )
        # Reset previously name-skipped employees so they can now be drafted with generic greeting
        await db.execute(
            "UPDATE employee SET draft_status = 'pending' WHERE draft_status = 'skipped' AND length(email_draft) = 0 AND (name = '' OR name IS NULL)"
        )
        # Auto-classify is_generic for existing rows that haven't been classified yet.
        # is_generic = 1: no real human name (null/blank/"None"/"null"/"n/a")
        # is_generic = 0: has a name — treat as named contact
        await db.execute(
            """UPDATE employee SET is_generic = CASE
                WHEN (COALESCE(TRIM(full_name), '') = '' OR LOWER(TRIM(full_name)) IN ('none', 'null', 'n/a'))
                 AND (COALESCE(TRIM(name), '') = '' OR LOWER(TRIM(name)) IN ('none', 'null', 'n/a'))
                THEN 1
                ELSE 0
               END
               WHERE is_generic IS NULL"""
        )
        await db.commit()


# -- Instance manager --------------------------------------------------------
instances = InstanceManager()


# -- Message types -----------------------------------------------------------

class MessageType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    IMAGE_GEN = "image_gen"


@dataclass
class QueuedMessage:
    chat_id: int
    msg_type: MessageType
    text: str = ""
    file_id: str = ""
    voice_reply: bool = False
    instance_id: int = 0  # 0 = use active instance
    user_id: int = 0


_processed_updates: set[int] = set()
_voice_reply_mode: bool = False  # Toggle: reply with voice to text messages too


# -- Per-instance queue workers ----------------------------------------------

def _ensure_worker(inst: Instance) -> None:
    """Start a queue worker for the instance if one isn't running."""
    if inst.worker_task is None or inst.worker_task.done():
        inst.worker_task = asyncio.create_task(_instance_queue_worker(inst))
        logger.info("Started worker for instance #%d: %s", inst.id, inst.title)


async def _instance_queue_worker(inst: Instance) -> None:
    """Persistent worker that processes queued messages for a single instance.

    Outer loop ensures the worker always restarts after any crash.
    """
    while True:
        try:
            item = await inst.queue.get()
        except asyncio.CancelledError:
            logger.info("Instance #%d worker cancelled while waiting", inst.id)
            return
        except Exception:
            logger.exception("Instance #%d queue.get() error", inst.id)
            await asyncio.sleep(1)
            continue

        inst.processing = True
        try:
            if item.msg_type == MessageType.TEXT:
                coro = _process_message(item.chat_id, item.text, voice_reply=item.voice_reply, instance=inst, user_id=item.user_id)
            elif item.msg_type == MessageType.PHOTO:
                coro = _process_photo_message(item.chat_id, item.file_id, item.text, instance=inst, user_id=item.user_id)
            elif item.msg_type == MessageType.VOICE:
                coro = _process_voice_message(item.chat_id, item.file_id, item.text, instance=inst, user_id=item.user_id)
            elif item.msg_type == MessageType.IMAGE_GEN:
                coro = _process_image_generation(item.chat_id, item.text)
            else:
                continue

            inst.current_task = asyncio.create_task(coro)
            await inst.current_task
        except asyncio.CancelledError:
            logger.info("Instance #%d task cancelled", inst.id)
        except Exception as e:
            logger.error("Instance #%d worker error processing %s: %s", inst.id, item.msg_type.value, e)
            try:
                await send_message(item.chat_id, f"Error processing your message: {e}")
            except Exception:
                logger.error("Instance #%d failed to send error message", inst.id)
        finally:
            inst.current_task = None
            inst.processing = False
            try:
                inst.queue.task_done()
            except ValueError:
                pass  # task_done called too many times


async def _enqueue_message(item: QueuedMessage) -> None:
    """Add a message to the target instance's queue."""
    inst = instances.get(item.instance_id) if item.instance_id else instances.active
    if inst is None:
        inst = instances.active

    _ensure_worker(inst)

    if inst.queue.full():
        enqueue_owner_id = 0 if item.user_id == ALLOWED_USER_ID else item.user_id
        owner_count = len(instances.list_all(for_owner_id=enqueue_owner_id))
        label = f" [#{instances.display_num(inst.id, enqueue_owner_id)}: {inst.title}]" if owner_count >= 2 else ""
        await send_message(
            item.chat_id,
            f"Queue is full (max 10){label}. Please wait or send /stop to cancel.",
        )
        return

    if inst.processing:
        position = inst.queue.qsize() + 1
        enqueue_owner_id = 0 if item.user_id == ALLOWED_USER_ID else item.user_id
        owner_count = len(instances.list_all(for_owner_id=enqueue_owner_id))
        label = f" [#{instances.display_num(inst.id, enqueue_owner_id)}: {inst.title}]" if owner_count >= 2 else ""
        await send_message(
            item.chat_id,
            f"Queued (position {position}){label}. I'll get to it when the current task finishes.",
        )

    await inst.queue.put(item)


def _is_any_processing() -> bool:
    """Check if any instance is currently processing."""
    return any(inst.processing for inst in instances.list_all())


def _total_queue_size() -> int:
    """Total pending messages across all instance queues."""
    return sum(inst.queue.qsize() for inst in instances.list_all() if inst.queue)


# -- Tunnel URL auto-sync ----------------------------------------------------

_N8N_API_URL = "https://n8n.srv1353882.hstgr.cloud/api/v1"
_N8N_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIzYTY2ODY1NC0zM2VlLTQ1ZWQtYjFiMi1kZjk0YTE2Y2I4MTciLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiMGMxZmYzM2EtZDk4ZS00M2E0LTg1YTYtNTY3MDFjNTYxZDI5IiwiaWF0IjoxNzcxMjY4OTM0fQ.6sPBilUtNcp7s4l7dTcxTQs8ZsjByYiK7ZyY8kT-epY"
_TUNNEL_METRICS_PORT = 20241
_N8N_WORKFLOW_IDS = [
    "9ZqdCitdrTLubmYU",  # Email Finder
    "LNSWwF6MGunycuwv",  # Email Campaign
    "7hkdg2kKxQMIPwTp",  # Job Scraper
]
_N8N_PUT_FIELDS = {"name", "nodes", "connections", "settings", "staticData"}


async def _get_tunnel_url() -> str:
    """Return current Cloudflare quick tunnel URL, or empty string if unavailable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{_TUNNEL_METRICS_PORT}/quicktunnel")
            if resp.status_code != 200:
                return ""
            hostname = resp.json().get("hostname")
            if not hostname:
                return ""
            return f"https://{hostname}"
    except Exception:
        return ""


async def _sync_tunnel_url() -> None:
    """Read current Cloudflare tunnel URL and patch it into all n8n workflows.

    Runs on server startup so any stale trycloudflare.com URLs get updated
    automatically without manual intervention.
    """
    try:
        tunnel_url = await _get_tunnel_url()
        if not tunnel_url:
            logger.warning("Tunnel sync: metrics not available (cloudflared not running?)")
            return
        logger.info("Tunnel sync: current URL = %s", tunnel_url)

        headers = {
            "X-N8N-API-KEY": _N8N_API_KEY,
            "Content-Type": "application/json",
        }
        updated_workflows = 0

        async with httpx.AsyncClient(timeout=30) as client:
            for wf_id in _N8N_WORKFLOW_IDS:
                resp = await client.get(f"{_N8N_API_URL}/workflows/{wf_id}", headers=headers)
                if resp.status_code != 200:
                    logger.warning("Tunnel sync: could not fetch workflow %s (%s)", wf_id, resp.status_code)
                    continue

                workflow = resp.json()
                nodes_changed = 0

                for node in workflow.get("nodes", []):
                    params = node.get("parameters", {})
                    url = params.get("url", "")
                    if isinstance(url, str) and "trycloudflare.com" in url and tunnel_url not in url:
                        params["url"] = re.sub(
                            r"https://[^/]+\.trycloudflare\.com", tunnel_url, url
                        )
                        nodes_changed += 1

                if nodes_changed == 0:
                    logger.info("Tunnel sync: workflow %s (%s) already up to date", wf_id, workflow.get("name", ""))
                    continue

                payload = {k: v for k, v in workflow.items() if k in _N8N_PUT_FIELDS}
                put_resp = await client.put(
                    f"{_N8N_API_URL}/workflows/{wf_id}",
                    headers=headers,
                    json=payload,
                )
                if put_resp.status_code in (200, 201):
                    logger.info(
                        "Tunnel sync: updated %d node(s) in '%s' (%s)",
                        nodes_changed, workflow.get("name", wf_id), wf_id,
                    )
                    updated_workflows += 1
                else:
                    logger.warning(
                        "Tunnel sync: PUT failed for %s: %s %s",
                        wf_id, put_resp.status_code, put_resp.text[:200],
                    )

        logger.info("Tunnel sync complete: %d workflow(s) updated", updated_workflows)
    except Exception as e:
        logger.warning("Tunnel sync failed (non-fatal): %s", e)


async def _init_memory_background() -> None:
    """Initialize vector memory without blocking API startup."""
    try:
        jefe_count = await memory_handler.index_files(0)
        secondary_count = await memory_handler.index_files(memory_handler.SECONDARY_USER_ID)
        logger.info("Memory initialized: %d chunks (primary) + %d chunks (secondary) indexed", jefe_count, secondary_count)
    except Exception as e:
        logger.warning("Memory initialization failed (non-fatal): %s", e)


async def _start_scheduler_background() -> None:
    """Start scheduler after startup has fully completed."""
    await asyncio.sleep(0.2)
    await scheduler.scheduler_loop()


async def _notify_startup_background() -> None:
    """Send startup ping without blocking server readiness."""
    await asyncio.sleep(0.2)
    await send_message(ALLOWED_USER_ID, "\u2705 Server restarted and ready.")


# -- Lifespan ----------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    errors = validate_config()
    for err in errors:
        logger.warning("Config issue: %s", err)
    if is_cli_available():
        logger.info("claude CLI found in PATH")
    else:
        logger.warning("claude CLI NOT found in PATH -- commands will fail")
    health.init()
    tunnel_url = await _get_tunnel_url()
    webhook_base = tunnel_url or WEBHOOK_URL
    if webhook_base:
        await register_webhook(webhook_base)
        if tunnel_url:
            logger.info("Webhook registered from live tunnel URL")
        else:
            logger.info("Webhook registered from WEBHOOK_URL env")
    else:
        logger.warning("WEBHOOK_URL not set -- webhook won't be auto-registered")

    # Start worker for the default instance (primary user)
    _ensure_worker(instances.active)

    # Auto-create dedicated instances for non-primary users and start their workers
    for uid in ALLOWED_USER_IDS:
        if uid == ALLOWED_USER_ID:
            continue
        name = USER_NAMES.get(uid, f"User {uid}")
        inst = instances.ensure_pinned(uid, name)
        _ensure_worker(inst)
        logger.info("Created dedicated instance for %s (user %d)", name, uid)

    logger.info("Instance workers started")

    # Initialize jobs database
    try:
        await _init_db()
        logger.info("Jobs DB initialized at %s", DB_PATH)
    except Exception as e:
        logger.warning("Jobs DB initialization failed (non-fatal): %s", e)

    # Seed default specialist agents
    agent_manager.ensure_default_agents()

    # Sync Cloudflare tunnel URL into n8n workflows
    await _sync_tunnel_url()

    # NOTE: Memory warmup is intentionally disabled here.
    # Chroma initialization can stall startup and block webhook responsiveness.
    logger.info("Telegram-Claude bridge is ready")
    asyncio.create_task(_start_scheduler_background())
    asyncio.create_task(_notify_startup_background())
    # Proactive worker does NOT auto-start — use /agent proactive start to enable
    yield
    # Clean up voice call if active
    from call_handler import end_call, get_manager
    if get_manager() and get_manager().is_active:
        await end_call()
    # Stop all instance workers
    for inst in instances.list_all():
        if inst.worker_task and not inst.worker_task.done():
            inst.worker_task.cancel()
            try:
                await inst.worker_task
            except asyncio.CancelledError:
                pass
    await proactive_worker.stop()
    await close_client()
    logger.info("Bridge shut down")


app = FastAPI(title="Telegram-Claude Bridge", lifespan=lifespan)


@app.get("/health")
async def health_endpoint():
    return health.get_health()


@app.get("/status")
async def status_endpoint():
    return health.get_status()


class DirectQueryRequest(BaseModel):
    prompt: str
    timeout_secs: int = 120


@app.post("/query")
async def direct_query(req: DirectQueryRequest):
    """Stateless AI query endpoint for automation tools (n8n, scripts).
    Runs Claude Haiku with no session/memory overhead. Returns raw text response."""
    try:
        response = await runner.run_query(req.prompt, timeout_secs=req.timeout_secs)
        return {"ok": True, "response": response}
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"ok": False, "error": f"AI response timed out after {req.timeout_secs}s", "response": ""},
        )
    except Exception as exc:
        logger.error("Direct query error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "response": ""},
        )


class AgentEmailResearchRequest(BaseModel):
    company: str
    company_domain: str
    title: str
    job_url: str
    location: str = ""
    tech_stack: str = ""


@app.post("/agent/email/research")
async def agent_email_research(req: AgentEmailResearchRequest):
    """Find hiring manager email via Claude Code web search (free, no Perplexity API cost)."""
    prompt = PROMPTS["hiring_manager_research"].format(
        company=req.company,
        company_domain=req.company_domain,
        title=req.title,
        job_url=req.job_url,
        location=req.location,
        tech_stack=req.tech_stack,
    )
    try:
        raw = await runner.run_query(prompt, timeout_secs=300)
        cleaned = raw.strip()
        s, e = cleaned.find("{"), cleaned.rfind("}")
        if s != -1 and e != -1:
            result = json.loads(cleaned[s:e + 1])
        else:
            raise ValueError("No JSON in Claude response")
    except Exception as exc:
        logger.error("Email research error: %s", exc)
        result = {
            "email": f"careers@{req.company_domain}",
            "confidence": "low",
            "reasoning": f"Research error: {exc}",
            "alternative_emails": [],
            "source_urls": [],
        }
    result.setdefault("email", f"careers@{req.company_domain}")
    result.setdefault("confidence", "low")
    result.setdefault("reasoning", "")
    result.setdefault("alternative_emails", [])
    result.setdefault("source_urls", [])
    return result
class AgentEmailDraftRequest(BaseModel):
    # Structured fields for the two-agent feedback loop (preferred)
    title: str = ""
    company: str = ""
    tech_stack: str = ""
    location: str = ""
    region: str = ""
    contact_name: str = ""
    mutual_context: str = ""
    personalization_hook: str = ""
    timeout_secs: int = 60
    # Legacy: raw prompt string (backward compat — used when title is not provided)
    prompt: str = ""


@app.post("/agent/email/draft")
async def agent_email_draft(req: AgentEmailDraftRequest):
    """Draft a cold outreach email via Claude Haiku (claude -p).
    Structured mode: two-agent feedback loop (Drafter + QA Tester), max 3 rounds, code-enforced.
    Legacy mode: raw prompt string passed directly (backward compat)."""
    try:
        # Legacy mode: raw prompt passed directly
        if req.prompt and not req.title:
            response_text = await runner.run_query(req.prompt, timeout_secs=req.timeout_secs)
            return {"ok": True, "response": response_text}

        # Structured mode: single pipeline via Claude Code subscription (no API billing)
        req_first_name = req.contact_name.split()[0] if req.contact_name else ""
        greeting = f"Hi {req_first_name}," if req_first_name else f"Hey {req.company} hiring team,"
        job_fields = {
            "title": req.title, "company": req.company, "tech_stack": req.tech_stack,
            "location": req.location, "region": req.region, "company_info": "",
        }
        contact_fields = {
            "contact_name": req.contact_name, "first_name": req_first_name,
            "greeting": greeting,
            "mutual_context": req.mutual_context, "personalization_hook": req.personalization_hook,
        }
        subagent_prompt = _build_email_subagent_prompt(job_fields, contact_fields)
        raw = await runner.run_query(subagent_prompt, timeout_secs=max(req.timeout_secs, 180))

        try:
            clean = raw.strip()
            start = clean.find("{")
            end   = clean.rfind("}") + 1
            if start != -1 and end > start:
                data = json.loads(clean[start:end])
                email = _normalize_email_paragraphs(data.get("email") or raw)
                return {"ok": True, "response": email, "rounds": data.get("rounds", 1)}
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

        return {"ok": True, "response": _normalize_email_paragraphs(raw), "rounds": 1}

    except Exception as exc:
        logger.error("Email draft loop error: %s", exc)
        return JSONResponse(status_code=500, content={"ok": False, "response": "", "error": str(exc)})


class AgentEmailNineRequest(BaseModel):
    employee_email: str                  # looks up employee + linked job from DB
    mutual_context: str = ""
    personalization_hook: str = ""
    timeout_secs: int = 120


def _normalize_email_paragraphs(email: str) -> str:
    """Ensure each sentence is its own paragraph separated by a blank line (rule 13).
    Handles emails that come back as a single flat string with no line breaks."""
    if not email:
        return email
    if isinstance(email, bytes):
        email = email.decode("utf-8", errors="replace")
    if "\n\n" in email:
        return email.strip()
    if "\n" in email:
        return re.sub(r"\n+", "\n\n", email).strip()
    # No newlines — split on sentence endings and rejoin
    sentences = re.split(r"(?<=[.?!])\s+", email.strip())
    return "\n\n".join(s.strip() for s in sentences if s.strip())


_GENERIC_EMAIL_PREFIXES = {
    "hr", "hiring", "info", "contact", "team", "jobs", "careers",
    "recruit", "talent", "hello", "admin", "support", "noreply", "no-reply",
}


def _first_name_from_email(email: str) -> str:
    """Extract a first name from emails with a dot/underscore separator like
    romia.nath@wipro.com → 'Romia' or ian.quiroga@azumo.co → 'Ian'.
    Single-word usernames (dchen, jlang) are too ambiguous — returns empty string.
    Also returns empty string for generic/role addresses."""
    if not email:
        return ""
    username = email.split("@")[0]
    # Require a separator — without one we can't reliably tell first name from
    # an initial+lastname slug like "dchen" or "jlang"
    if "." not in username and "_" not in username:
        return ""
    first_part = username.split(".")[0].split("_")[0].split("+")[0]
    first_lower = first_part.lower()
    if len(first_part) < 2 or first_lower in _GENERIC_EMAIL_PREFIXES:
        return ""
    # Reject company-handle patterns
    for suffix in ("careers", "hiring", "jobs", "recruit", "talent", "hr", "info", "team"):
        if first_lower.endswith(suffix) and len(first_lower) > len(suffix):
            return ""
    return first_part.capitalize()


def _build_email_subagent_prompt(job_fields: dict, contact_fields: dict) -> str:
    """Build self-contained instructions for one email pipeline sub-agent.
    Sub-agent handles the full drafter → tester → detector sequence internally.
    No tools used — pure text reasoning. Returns JSON."""
    title        = job_fields["title"]
    company      = job_fields["company"]
    tech_stack   = job_fields.get("tech_stack", "")
    location     = job_fields.get("location", "")
    region       = job_fields.get("region", "")
    company_info = job_fields.get("company_info", "")
    greeting     = contact_fields.get("greeting", "")
    contact_name = contact_fields.get("contact_name", "")
    first_name   = contact_fields.get("first_name", contact_name.split()[0] if contact_name else "")
    mutual_ctx   = contact_fields.get("mutual_context", "")
    hook         = contact_fields.get("personalization_hook", "")

    if mutual_ctx:
        path_desc  = "PATH A — mutual_context is provided: write one genuinely enthusiastic sentence about the shared connection. Express real curiosity or admiration, not a flat acknowledgment."
        path_label = "PATH A"
        sent_count = "5"
        ctx_note   = "provided"
    elif hook:
        path_desc  = "PATH B — personalization_hook is provided: write one sentence expressing genuine enthusiasm or curiosity about something specific in their story or work."
        path_label = "PATH B"
        sent_count = "5"
        ctx_note   = "provided"
    else:
        path_desc  = "PATH C — both are empty: skip sentence 2 entirely, go straight to sentence 3."
        path_label = "PATH C"
        sent_count = "4"
        ctx_note   = "not provided"

    return (
        "You are running one cold email drafting pipeline. Complete all 3 steps and return JSON. "
        "Do not use any tools — pure text reasoning only.\n\n"

        "JOB CONTEXT:\n"
        f"Title: {title} at {company}\n"
        f"Tech stack: {tech_stack}\n"
        f"Location: {location}\n"
        f"Region: {region}\n"
        f"Contact full name (from DB): {contact_name}\n"
        f"Contact first name (use for any in-body references): {first_name}\n"
        f"Mutual context: {mutual_ctx}\n"
        f"Personalization hook: {hook}\n"
        f"Company info: {company_info}\n\n"

        f"CANDIDATE BACKGROUND:\n{USER_BACKGROUND}\n\n"

        f"TRUE FACTS (never contradict): {USER_TRUE_FACTS}\n\n"

        "---\n\n"

        "STEP 1 — DRAFT THE EMAIL:\n\n"
        "Rules:\n"
        "- No greeting line — start directly with Sentence 1\n"
        "- No subject line, no sign-off\n"
        "- Under 130 words total\n"
        "- No dashes of any kind (em dash, en dash, hyphen as dash — zero)\n"
        "- No filler phrases: 'I am excited', 'I would be a great fit', 'I am passionate', "
        "'I look forward to', 'I would love to'\n"
        "- Each sentence is its own paragraph separated by a blank line\n"
        "- Sound like a real person, not a template\n\n"

        "Structure (exact order):\n"
        f"Sentence 1: \"My name is {USER_FULL_NAME}, a software engineer reaching out about the {title} opening at {company}.\"\n"
        f"Sentence 2 — {path_desc}\n"
        f"Sentence 3: Why the candidate specifically wants to work at {company}. "
        "Reference something real and specific to this company. Express clear intention to be there, not just admiration.\n"
        "Sentence 4: Most relevant concrete experience — name the actual thing built or shipped, not a category.\n"
        "Sentence 5: Ask if they have time for a quick call.\n\n"

        "---\n\n"

        "STEP 2 — SELF-TEST (max 3 rounds):\n"
        "Check your draft against all 14 rules:\n"
        "1. No subject line\n"
        "2. First line is Sentence 1 — no greeting before it\n"
        "3. No sign-off\n"
        "4. Under 130 words\n"
        "5. Zero dashes\n"
        "6. None of the banned filler phrases\n"
        "7. Sentence 1 introduces the candidate + role + company\n"
        f"8. Sentence 2 follows {path_label}\n"
        f"9. Sentence 3 is specific to {company} (not a generic industry statement)\n"
        "10. Sentence 4 names an actual project or deliverable\n"
        "11. Sentence 5 is a CTA for a call\n"
        f"12. Sentence count: {sent_count} (mutual_context {ctx_note})\n"
        "13. Each sentence is its own paragraph\n"
        f"14. Sentence 3 expresses intention to work at {company}, not just admiration\n\n"
        "If any fail, revise and recheck. Max 3 rounds total.\n\n"

        "---\n\n"

        "STEP 3 — AI DETECTION + FACT CHECK:\n"
        "Check for AI writing tells: overly smooth structure, assembled phrases, abstract framing, "
        "transitions too clean, phrases no human would say out loud, hollow impressive sentences.\n"
        "Verify all facts against TRUE FACTS listed above.\n"
        "If AI writing detected OR facts wrong: rewrite to fix. Keep same structure, under 130 words, zero dashes, no filler.\n\n"

        "Return ONLY valid JSON, no other text, no markdown:\n"
        '{"email": "<final complete email starting with Sentence 1>", '
        '"ai_detected": true or false, "fact_violations": [], "rounds": 1}'
    )


def _build_generic_email_subagent_prompt(job_fields: dict) -> str:
    """Build a self-contained 3-step prompt for generic company inboxes (Path 2).
    No contact name available — uses fixed Sentence 1 formula + company-specific content.
    Returns JSON: {email, ai_detected, fact_violations, rounds}."""
    return _PROMPT_EMAIL_DRAFT_GENERIC.format(
        title=job_fields["title"],
        company=job_fields["company"],
        tech_stack=job_fields.get("tech_stack", ""),
        location=job_fields.get("location", ""),
        region=job_fields.get("region", "US"),
        company_info=job_fields.get("company_info", ""),
    )


# ---------------------------------------------------------------------------
# Pipeline status detection — module-level so all write paths can use it
# ---------------------------------------------------------------------------

_PIPELINE_TELLS: list[str] = [
    "pipelines ran concurrently",
    "All 3 pipelines",
    "All 3 drafts",
    "sub-agent pipelines",
    "pipelines simultaneously",
    "groups 2 and 3 passed",
    "ran in parallel and completed",
    "drafts completed concurrently",
    "drafts returned clean",
    "zero fact violations",
    "zero AI detection",
    "AI detection flags",
    "Group 3 is the longest",
    "Group 1 is the most concise",
    "all completed in",
    "Notification sent to Telegram",
    "PATH C, no sentence",
    "zero dashes, no filler",
    "facts verified",
]


def _is_pipeline_status(text: str) -> bool:
    """Return True if text looks like a pipeline status report, not an actual email body.
    All valid emails begin with 'My name is' (Sentence 1)."""
    if not text:
        return False
    # Must start with "My name is" — all valid emails begin with Sentence 1
    _first_name = USER_FULL_NAME.split()[0] if USER_FULL_NAME else "My name is"
    if not text.strip().startswith(f"My name is {_first_name}"):
        first_line = text.strip().split("\n")[0].lower()
        if "my name is" not in first_line and USER_FULL_NAME.lower() not in first_line:
            return True
    return any(tell in text for tell in _PIPELINE_TELLS)


async def _run_email_orchestrator(
    job_fields: dict,
    contact_fields: dict,
    timeout_secs: int = 600,
) -> dict:
    """9-agent email pipeline via Claude Code Task tool — zero API billing.
    One claude -p call spawns 3 parallel sub-agents via Task tool.
    Each sub-agent runs the full drafter → tester → detector pipeline internally."""
    subagent = _build_email_subagent_prompt(job_fields, contact_fields)

    orchestrator_prompt = (
        "You are an email drafting orchestrator. Use the Task tool to launch exactly 3 parallel "
        "sub-agent pipelines simultaneously.\n\n"
        "CRITICAL: Launch all 3 tasks at the same time in a single parallel batch. "
        "Do NOT run them sequentially — they must run concurrently.\n\n"
        "Give each sub-agent these identical instructions:\n\n"
        "---\n"
        f"{subagent}\n"
        "---\n\n"
        "After all 3 complete, collect their JSON results and return ONLY the following JSON — "
        "no other text, no markdown:\n"
        '{"results": ['
        '{"group": 1, "email": "<email from task 1>", "ai_detected": false, "fact_violations": [], "rounds": 1}, '
        '{"group": 2, "email": "<email from task 2>", "ai_detected": false, "fact_violations": [], "rounds": 1}, '
        '{"group": 3, "email": "<email from task 3>", "ai_detected": false, "fact_violations": [], "rounds": 1}'
        "]}\n\n"
        "Replace placeholder values with actual results from each sub-agent."
    )

    raw = await runner.run_query(orchestrator_prompt, timeout_secs=timeout_secs)

    # _PIPELINE_TELLS and _is_pipeline_status are module-level (defined above _run_email_orchestrator)
    try:
        clean = raw.strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start != -1 and end > start:
            data    = json.loads(clean[start:end])
            results = data.get("results", [])
            # Validate each result's email field — reject any that contain status/process text
            valid_results = [r for r in results if r.get("email") and not _is_pipeline_status(r["email"])]
            if valid_results:
                return _pick_best(valid_results)
            if results and not valid_results:
                # All results were status reports — raise so batch falls back to single pipeline
                raise ValueError(
                    f"Orchestrator returned pipeline status text in JSON email fields: "
                    f"{results[0].get('email', '')[:200]}"
                )
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Orchestrator JSON parse failed: %s", exc)

    # Fallback: treat raw as the email — but reject pipeline status summaries
    if _is_pipeline_status(raw):
        raise ValueError(f"Orchestrator returned pipeline status text instead of email: {raw[:200]}")

    return {
        "group": 1,
        "email": _normalize_email_paragraphs(raw),
        "ai_detected": False,
        "fact_violations": [],
        "rounds": 1,
    }


async def _run_email_group(
    group_id: int,
    prompts: dict,
    job_fields: dict,
    contact_fields: dict,
    timeout_secs: int,
) -> dict:
    """Run one 3-agent pipeline: Drafter → QA Tester (up to 3 rounds) → AI Detector."""
    draft = ""
    feedback = ""
    rounds_taken = 0

    # --- Agents 1 + 2: Drafter / Tester loop ---
    for round_num in range(1, 4):
        rounds_taken = round_num
        feedback_section = f"QA feedback — fix only the flagged violations:\n{feedback}" if feedback else ""

        drafter_prompt = prompts["drafter"].format(
            **job_fields,
            **contact_fields,
            feedback=feedback_section,
        )
        draft = await runner.run_query(drafter_prompt, timeout_secs=timeout_secs)

        tester_prompt = prompts["tester"].format(
            draft_email=draft,
            greeting=contact_fields.get("greeting", ""),
            mutual_context=contact_fields["mutual_context"],
            personalization_hook=contact_fields["personalization_hook"],
            title=job_fields["title"],
            company=job_fields["company"],
            round=round_num,
        )
        test_raw = await runner.run_query(tester_prompt, timeout_secs=timeout_secs)

        try:
            clean = test_raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            qa = json.loads(clean)
            if qa.get("verdict") == "PASS":
                break
            violations = qa.get("violations", [])
            if qa.get("corrected_email"):
                draft = qa["corrected_email"]
            if round_num < 3 and violations:
                feedback = "\n".join(
                    f"- Rule {v.get('rule', '?')}: {v.get('description', '')}" for v in violations
                )
        except (json.JSONDecodeError, KeyError, AttributeError):
            break

    # --- Agent 3: AI Detector + Fact Checker ---
    detector_prompt = prompts["ai_detector"].format(draft_email=draft)
    detector_raw = await runner.run_query(detector_prompt, timeout_secs=timeout_secs)

    try:
        clean = detector_raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        det = json.loads(clean)
        final_email = _normalize_email_paragraphs(det.get("final_email") or draft)
        ai_detected = det.get("ai_detected", False)
        fact_violations = det.get("fact_violations", [])
        ai_tells = det.get("ai_tells", [])
    except (json.JSONDecodeError, KeyError, AttributeError):
        final_email = _normalize_email_paragraphs(draft)
        ai_detected = False
        fact_violations = []
        ai_tells = []

    return {
        "group": group_id,
        "email": final_email,
        "rounds": rounds_taken,
        "ai_detected": ai_detected,
        "fact_violations": fact_violations,
        "ai_tells": ai_tells,
    }


def _pick_best(results: list[dict]) -> dict:
    """Pick the cleanest result: prefer no AI detected + no fact violations."""
    fully_clean = [r for r in results if not r["ai_detected"] and not r["fact_violations"]]
    if fully_clean:
        return fully_clean[0]
    no_facts = [r for r in results if not r["fact_violations"]]
    if no_facts:
        return no_facts[0]
    return results[0]


@app.post("/agent/email/draft/nine")
async def agent_email_draft_nine(req: AgentEmailNineRequest):
    """9-agent email drafting system: 3 parallel groups × 3 agents each.
    Each group runs: Drafter → QA Tester (up to 3 rounds) → AI Detector/Humanizer.
    Pulls employee + job context from DB. Stores best result in employee.email_draft."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Fetch employee
        cur = await db.execute(
            "SELECT email, name, full_name, title, company, linkedin_url, info, is_generic FROM employee WHERE email = ?",
            (req.employee_email,),
        )
        emp = await cur.fetchone()
        if not emp:
            return JSONResponse(status_code=404, content={"ok": False, "error": f"Employee not found: {req.employee_email}"})

        # Fetch linked job via job_employee join, fall back to company name match
        cur = await db.execute(
            """SELECT j.title, j.company, j.tech_stack, j.location, j.source
               FROM jobs j
               JOIN job_employee je ON j.job_id = je.job_id
               WHERE je.email = ? LIMIT 1""",
            (req.employee_email,),
        )
        job = await cur.fetchone()
        if not job:
            # Fallback: match by company name
            cur = await db.execute(
                "SELECT title, company, tech_stack, location, source FROM jobs WHERE company = ? LIMIT 1",
                (emp["company"],),
            )
            job = await cur.fetchone()

        if not job:
            return JSONResponse(status_code=404, content={"ok": False, "error": f"No job found for company: {emp['company']}"})

    job_fields = {
        "title":        job["title"],
        "company":      job["company"],
        "tech_stack":   job["tech_stack"] or "",
        "location":     job["location"] or "",
        "region":       "PL" if job["source"] and "pl" in job["source"].lower() else "US",
        "company_info": emp["info"] or "",
    }

    if _is_named_contact(emp):
        # Path 1 — named contact: full personalization 9-agent pipeline
        full_name    = emp["full_name"] or emp["name"] or ""
        first_name   = full_name.split()[0] if full_name else ""
        contact_name = full_name
        company_name = job["company"] or emp["company"] or "hiring team"
        greeting     = f"Hi {first_name}," if first_name else f"Hi {company_name} Hiring Team,"
        contact_fields = {
            "contact_name":         contact_name,
            "first_name":           first_name,
            "greeting":             greeting,
            "mutual_context":       req.mutual_context,
            "personalization_hook": req.personalization_hook,
        }
        try:
            best = await _run_email_orchestrator(job_fields, contact_fields, timeout_secs=req.timeout_secs)
        except Exception as exc:
            logger.error("Nine-agent draft error: %s", exc)
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
        display_name = contact_name
    else:
        # Path 2 — generic inbox: single self-contained pipeline, no person name
        greeting = f"Hi {job['company']} Hiring Team,"
        generic_prompt = _build_generic_email_subagent_prompt(job_fields)
        try:
            raw_g = await runner.run_query(generic_prompt, timeout_secs=req.timeout_secs)
            clean_g = raw_g.strip()
            s = clean_g.find("{")
            e = clean_g.rfind("}") + 1
            g_data = json.loads(clean_g[s:e]) if s != -1 and e > s else {}
            g_email = g_data.get("email") or raw_g
        except Exception as exc:
            logger.error("Generic draft error: %s", exc)
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
        g_email_normalized = _normalize_email_paragraphs(g_email)
        if _is_pipeline_status(g_email_normalized):
            logger.error("Generic draft returned pipeline status text: %s", g_email_normalized[:200])
            return JSONResponse(status_code=500, content={"ok": False, "error": f"Generic draft returned pipeline status text: {g_email_normalized[:120]}"})
        best = {
            "group": 1,
            "email": g_email_normalized,
            "ai_detected": False,
            "fact_violations": [],
            "rounds": 1,
        }
        display_name = job["company"]

    # Final guard: never write pipeline status text to the DB
    if _is_pipeline_status(best["email"]):
        logger.error("nine-agent endpoint: pipeline status text would be stored as email_draft: %s", best["email"][:200])
        return JSONResponse(status_code=500, content={"ok": False, "error": f"Draft validation failed: pipeline status text detected in output: {best['email'][:120]}"})

    # Store best in employee.email_draft + greeting (WF3 prepends greeting before sending)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE employee SET email_draft = ?, greeting = ?, draft_status = 'done' WHERE email = ?",
            (best["email"], greeting, req.employee_email),
        )
        await db.commit()

    return {
        "ok": True,
        "path": "named" if _is_named_contact(emp) else "generic",
        "employee": display_name,
        "greeting": greeting,
        "company": job_fields["company"],
        "title": job_fields["title"],
        "best": best,
    }


# -- Email draft batch -------------------------------------------------------

_batch_running: bool = False


async def _run_email_batch(workers: int, chat_id: int, limit: int = 0) -> None:
    """Process all named employees with no draft using N parallel workers."""
    global _batch_running
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT email FROM employee
                   WHERE draft_status = 'pending'
                   ORDER BY created_at"""
            )
            rows = await cur.fetchall()
        emails = [r["email"] for r in rows]
        if limit > 0:
            emails = emails[:limit]
        total = len(emails)

        if total == 0:
            await send_message(chat_id, "No pending employees to draft.")
            return

        await send_message(chat_id, f"Batch started — {total} emails queued, {workers} workers")

        sem = asyncio.Semaphore(workers)
        done = 0
        errors = 0

        async def _process_one(emp_email: str) -> None:
            nonlocal done, errors
            async with sem:
                try:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE employee SET draft_status = 'drafting' WHERE email = ?",
                            (emp_email,),
                        )
                        await db.commit()

                    async with aiosqlite.connect(DB_PATH) as db:
                        db.row_factory = aiosqlite.Row
                        cur = await db.execute(
                            "SELECT email, name, full_name, title, company, linkedin_url, info, is_generic FROM employee WHERE email = ?",
                            (emp_email,),
                        )
                        emp = await cur.fetchone()
                        if not emp:
                            errors += 1
                            return
                        cur = await db.execute(
                            """SELECT j.title, j.company, j.tech_stack, j.location, j.source
                               FROM jobs j
                               JOIN job_employee je ON j.job_id = je.job_id
                               WHERE je.email = ? LIMIT 1""",
                            (emp_email,),
                        )
                        job = await cur.fetchone()
                        if not job:
                            cur = await db.execute(
                                "SELECT title, company, tech_stack, location, source FROM jobs WHERE company = ? LIMIT 1",
                                (emp["company"],),
                            )
                            job = await cur.fetchone()

                    if not job:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                "UPDATE employee SET draft_status = 'skipped' WHERE email = ?",
                                (emp_email,),
                            )
                            await db.commit()
                        errors += 1
                        return

                    job_fields = {
                        "title":        job["title"],
                        "company":      job["company"],
                        "tech_stack":   job["tech_stack"] or "",
                        "location":     job["location"] or "",
                        "region":       "PL" if job["source"] and "pl" in job["source"].lower() else "US",
                        "company_info": emp["info"] or "",
                    }

                    if _is_named_contact(emp):
                        # Path 1 — named contact: full personalization 9-agent pipeline
                        full_name    = emp["full_name"] or emp["name"] or ""
                        first_name   = full_name.split()[0] if full_name else ""
                        contact_name = full_name
                        company_name = job["company"] or emp["company"] or "hiring team"
                        greeting     = f"Hi {first_name}," if first_name else f"Hi {company_name} Hiring Team,"
                        contact_fields = {
                            "contact_name":         contact_name,
                            "first_name":           first_name,
                            "greeting":             greeting,
                            "mutual_context":        "",
                            "personalization_hook":  "",
                        }
                        try:
                            best = await _run_email_orchestrator(job_fields, contact_fields, timeout_secs=600)
                        except ValueError:
                            # Orchestrator returned garbage — fall back to single named pipeline
                            subagent_prompt = _build_email_subagent_prompt(job_fields, contact_fields)
                            raw_fb = await runner.run_query(subagent_prompt, timeout_secs=300)
                            try:
                                clean_fb = raw_fb.strip()
                                s = clean_fb.find("{")
                                e = clean_fb.rfind("}") + 1
                                fb_data = json.loads(clean_fb[s:e]) if s != -1 and e > s else {}
                                fb_email = fb_data.get("email") or raw_fb
                            except (json.JSONDecodeError, KeyError, ValueError):
                                fb_email = raw_fb
                            fb_email = _normalize_email_paragraphs(fb_email)
                            if _is_pipeline_status(fb_email):
                                raise ValueError(f"Fallback also returned pipeline status text: {fb_email[:120]}")
                            best = {
                                "group": 1,
                                "email": fb_email,
                                "ai_detected": False,
                                "fact_violations": [],
                                "rounds": 1,
                            }
                    else:
                        # Path 2 — generic inbox: single self-contained pipeline, no person name
                        greeting = f"Hi {job['company']} Hiring Team,"
                        generic_prompt = _build_generic_email_subagent_prompt(job_fields)
                        raw_g = await runner.run_query(generic_prompt, timeout_secs=300)
                        try:
                            clean_g = raw_g.strip()
                            s = clean_g.find("{")
                            e = clean_g.rfind("}") + 1
                            g_data = json.loads(clean_g[s:e]) if s != -1 and e > s else {}
                            g_email = g_data.get("email") or raw_g
                        except (json.JSONDecodeError, KeyError, ValueError):
                            g_email = raw_g
                        g_email_normalized = _normalize_email_paragraphs(g_email)
                        if _is_pipeline_status(g_email_normalized):
                            raise ValueError(f"Generic draft returned pipeline status text: {g_email_normalized[:120]}")
                        best = {
                            "group": 1,
                            "email": g_email_normalized,
                            "ai_detected": False,
                            "fact_violations": [],
                            "rounds": 1,
                        }

                    # Final guard: never write pipeline status text to the DB
                    if _is_pipeline_status(best["email"]):
                        raise ValueError(f"Refusing to store pipeline status text as email_draft: {best['email'][:120]}")
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE employee SET email_draft = ?, greeting = ?, draft_status = 'done' WHERE email = ?",
                            (best["email"], greeting, emp_email),
                        )
                        await db.commit()

                    done += 1
                    if done % 10 == 0:
                        await send_message(chat_id, f"[{done}/{total}] drafts complete...")

                except Exception as exc:
                    logger.error("Batch draft error for %s: %s", emp_email, exc)
                    errors += 1
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE employee SET draft_status = 'pending' WHERE email = ?",
                            (emp_email,),
                        )
                        await db.commit()

        await asyncio.gather(*[_process_one(e) for e in emails])
        await send_message(
            chat_id,
            f"Batch complete!\n\nDone: {done}\nErrors (retryable): {errors}\nTotal: {total}",
        )

    finally:
        _batch_running = False


class AgentEmailBatchRequest(BaseModel):
    workers: int = 3
    limit: int = 0   # 0 = all pending


@app.post("/agent/email/draft/batch")
async def agent_email_draft_batch(req: AgentEmailBatchRequest = None):
    """Start a batch email draft job for all named employees with no draft.
    Runs in the background. Sends Telegram progress updates."""
    global _batch_running
    if req is None:
        req = AgentEmailBatchRequest()
    if _batch_running:
        return JSONResponse(status_code=409, content={"ok": False, "error": "Batch already running"})

    workers = max(1, min(req.workers, 9))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) as cnt FROM employee WHERE draft_status = 'pending'")
        row = await cur.fetchone()
        pending = row["cnt"]

    if pending == 0:
        return {"ok": True, "message": "No pending employees to draft", "queued": 0}

    _batch_running = True
    asyncio.create_task(_run_email_batch(workers, ALLOWED_USER_ID, req.limit))
    return {"ok": True, "queued": pending, "workers": workers}


@app.get("/agent/email/draft/batch/status")
async def agent_email_draft_batch_status():
    """Live progress of the email draft batch job."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT draft_status, COUNT(*) as cnt FROM employee GROUP BY draft_status"
        )
        rows = await cur.fetchall()
    counts = {r["draft_status"]: r["cnt"] for r in rows}
    return {
        "ok":           True,
        "batch_running": _batch_running,
        "pending":      counts.get("pending", 0),
        "drafting":     counts.get("drafting", 0),
        "done":         counts.get("done", 0),
        "skipped":      counts.get("skipped", 0),
    }


# -- Email draft audit -------------------------------------------------------

_audit_running: bool = False


def _is_substantive_info(info_str: str) -> bool:
    """Return True if the info JSON contains real research data (not just status flags)."""
    if not info_str:
        return False
    try:
        data = json.loads(info_str)
    except (json.JSONDecodeError, ValueError):
        return bool(info_str.strip())
    research_keys = {"current_role_summary", "personalization_hook", "mutual_context", "career_background", "recent_activity"}
    return bool(research_keys & set(data.keys()))


def _is_named_contact(emp) -> bool:
    """Return True if employee has a real human name (Path 1 — personalized pipeline).
    Returns False for generic/role-based inboxes with no name (Path 2 — generic pipeline).
    Checks is_generic DB field first; falls back to inspecting name fields."""
    # Prefer the explicit DB flag if it's been set
    if emp["is_generic"] is not None:
        return emp["is_generic"] == 0
    # Fallback: derive from name fields
    name = (emp["full_name"] or emp["name"] or "").strip()
    return bool(name) and name.lower() not in {"none", "null", "n/a", ""}


def _build_audit_prompt(info_json: str, email_draft: str) -> str:
    """Build a run_claude_code prompt to audit whether research data is used in the email."""
    return (
        "You are auditing a cold outreach email draft for personalization quality.\n"
        "Do not use any tools — pure text reasoning only.\n\n"
        "RESEARCH DATA (from DB):\n"
        f"{info_json}\n\n"
        "EMAIL DRAFT:\n"
        f"{email_draft}\n\n"
        "TASK — check these 3 things:\n"
        "1. Does the email body reference anything specific from the research?\n"
        "   (hiring manager background, recent company activity, personalization hook, mutual context)\n"
        "2. Is the personalization_hook from the research used or paraphrased anywhere in the email?\n"
        "3. Does the company sentence show genuine specific interest or is it a generic industry statement?\n\n"
        "Return ONLY valid JSON, no other text, no markdown:\n"
        '{"research_used": true, "hook_reflected": true, "evidence": "what was used or nothing specific", "flagged": false, "flag_reason": ""}'
    )


async def _redraft_flagged(workers: int, chat_id: int) -> None:
    """Re-draft all real_info_unused employees, extracting hooks properly from their info JSON."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT email, name, full_name, company, info FROM employee WHERE audit_status = 'real_info_unused'"
        )
        rows = await cur.fetchall()
    employees = list(rows)
    total = len(employees)

    if total == 0:
        await send_message(chat_id, "No flagged employees to re-draft.")
        return

    await send_message(chat_id, f"Re-drafting {total} flagged employees with extracted hooks...")
    sem = asyncio.Semaphore(workers)
    done = 0
    errors = 0

    async def _redraft_one(emp) -> None:
        nonlocal done, errors
        async with sem:
            try:
                emp_email = emp["email"]
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    cur = await db.execute(
                        """SELECT j.title, j.company, j.tech_stack, j.location, j.source
                           FROM jobs j
                           JOIN job_employee je ON j.job_id = je.job_id
                           WHERE je.email = ? LIMIT 1""",
                        (emp_email,),
                    )
                    job = await cur.fetchone()
                    if not job:
                        cur = await db.execute(
                            "SELECT title, company, tech_stack, location, source FROM jobs WHERE company = ? LIMIT 1",
                            (emp["company"],),
                        )
                        job = await cur.fetchone()

                if not job:
                    errors += 1
                    return

                # Extract hooks directly from info JSON — the key fix
                info_str = emp["info"] or ""
                personalization_hook = ""
                mutual_context = ""
                try:
                    info_data = json.loads(info_str)
                    personalization_hook = info_data.get("personalization_hook", "")
                    mutual_context = info_data.get("mutual_context", "")
                except Exception:
                    pass

                full_name    = emp["full_name"] or emp["name"] or ""
                first_name   = full_name.split()[0] if full_name else ""
                contact_name = full_name
                company_name = job["company"] or emp["company"] or "hiring team"
                greeting = f"Hi {first_name}," if first_name else f"Hey {company_name} hiring team,"
                job_fields = {
                    "title":        job["title"],
                    "company":      job["company"],
                    "tech_stack":   job["tech_stack"] or "",
                    "location":     job["location"] or "",
                    "region":       "PL" if job["source"] and "pl" in job["source"].lower() else "US",
                    "company_info": info_str,
                }
                contact_fields = {
                    "contact_name":         contact_name,
                    "first_name":           first_name,
                    "greeting":             greeting,
                    "mutual_context":       mutual_context,
                    "personalization_hook": personalization_hook,
                }

                try:
                    best = await _run_email_orchestrator(job_fields, contact_fields, timeout_secs=600)
                except ValueError:
                    subagent_prompt = _build_email_subagent_prompt(job_fields, contact_fields)
                    raw_fb = await runner.run_query(subagent_prompt, timeout_secs=300)
                    try:
                        clean_fb = raw_fb.strip()
                        s = clean_fb.find("{")
                        e = clean_fb.rfind("}") + 1
                        fb_data = json.loads(clean_fb[s:e]) if s != -1 and e > s else {}
                        fb_email = fb_data.get("email") or raw_fb
                    except (json.JSONDecodeError, KeyError, ValueError):
                        fb_email = raw_fb
                    fb_email_normalized = _normalize_email_paragraphs(fb_email)
                    if _is_pipeline_status(fb_email_normalized):
                        raise ValueError(f"Re-draft fallback returned pipeline status text: {fb_email_normalized[:120]}")
                    best = {
                        "group": 1,
                        "email": fb_email_normalized,
                        "ai_detected": False,
                        "fact_violations": [],
                        "rounds": 1,
                    }

                # Final guard: never write pipeline status text to the DB
                if _is_pipeline_status(best["email"]):
                    raise ValueError(f"Refusing to store pipeline status text as email_draft: {best['email'][:120]}")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE employee SET email_draft = ?, draft_status = 'done', "
                        "audit_status = 'real_info_used', audit_notes = 're-drafted with extracted hooks' "
                        "WHERE email = ?",
                        (best["email"], emp_email),
                    )
                    await db.commit()

                done += 1
                if done % 10 == 0:
                    await send_message(chat_id, f"[Re-draft {done}/{total}] done...")

            except Exception as exc:
                logger.error("Re-draft error for %s: %s", emp["email"], exc)
                errors += 1

    await asyncio.gather(*[_redraft_one(e) for e in employees])
    await send_message(
        chat_id,
        f"Re-draft complete!\n\nDone: {done}\nErrors: {errors}\nTotal: {total}",
    )


async def _run_email_audit(workers: int, chat_id: int) -> None:
    """Audit all done email drafts: categorize info quality, LLM-check substantive ones,
    then auto re-draft any flagged (real_info_unused) employees with extracted hooks."""
    global _audit_running
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT email, name, full_name, company, info, email_draft FROM employee WHERE draft_status = 'done'"
            )
            rows = await cur.fetchall()
        employees = list(rows)
        total = len(employees)

        if total == 0:
            await send_message(chat_id, "No done employees to audit.")
            return

        await send_message(chat_id, f"Audit started — {total} drafts to check, {workers} workers")
        sem = asyncio.Semaphore(workers)
        checked = 0
        flagged_list: list = []
        counts: dict = {"no_contact_info": 0, "has_name_no_research": 0, "real_info_used": 0, "real_info_unused": 0}

        async def _audit_one(emp) -> None:
            nonlocal checked
            async with sem:
                try:
                    email_addr  = emp["email"]
                    has_name    = bool(emp["full_name"] or emp["name"])
                    info_str    = emp["info"] or ""
                    email_draft = emp["email_draft"] or ""
                    has_substantive = _is_substantive_info(info_str)

                    # Fast-path: no research data — categorize without LLM
                    if not has_substantive:
                        status = "has_name_no_research" if has_name else "no_contact_info"
                        counts[status] += 1
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                "UPDATE employee SET audit_status = ?, audit_notes = '' WHERE email = ?",
                                (status, email_addr),
                            )
                            await db.commit()
                        checked += 1
                        return

                    # Mark in-progress so status endpoint reflects it
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE employee SET audit_status = 'auditing' WHERE email = ?",
                            (email_addr,),
                        )
                        await db.commit()

                    # LLM check via run_claude_code — zero API cost
                    prompt = _build_audit_prompt(info_str, email_draft)
                    raw = await runner.run_query(prompt, timeout_secs=180)
                    result: dict = {}
                    try:
                        clean = raw.strip()
                        s = clean.find("{")
                        e = clean.rfind("}") + 1
                        if s != -1 and e > s:
                            result = json.loads(clean[s:e])
                    except (json.JSONDecodeError, ValueError):
                        pass

                    is_flagged = result.get("flagged", False) or (
                        not result.get("research_used", True) and not result.get("hook_reflected", True)
                    )
                    status = "real_info_unused" if is_flagged else "real_info_used"
                    notes  = result.get("flag_reason", "") or result.get("evidence", "")
                    counts[status] += 1

                    if is_flagged:
                        hook_preview = ""
                        try:
                            info_data    = json.loads(info_str)
                            hook_preview = info_data.get("personalization_hook") or info_data.get("mutual_context") or ""
                        except Exception:
                            pass
                        flagged_list.append({
                            "email":        email_addr,
                            "company":      emp["company"],
                            "hook_in_info": hook_preview[:120],
                            "draft_preview": email_draft[:150],
                            "flag_reason":  notes[:200],
                        })

                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE employee SET audit_status = ?, audit_notes = ? WHERE email = ?",
                            (status, notes[:500], email_addr),
                        )
                        await db.commit()

                    checked += 1
                    if checked % 20 == 0:
                        await send_message(chat_id, f"[{checked}/{total}] audited...")

                except Exception as exc:
                    logger.error("Audit error for %s: %s", emp["email"], exc)
                    checked += 1

        await asyncio.gather(*[_audit_one(e) for e in employees])

        summary = (
            f"Audit complete! {total} drafts checked.\n\n"
            f"No info/name: {counts['no_contact_info']}\n"
            f"Name only, no research: {counts['has_name_no_research']}\n"
            f"Research used: {counts['real_info_used']}\n"
            f"Research NOT used (flagged): {counts['real_info_unused']}\n\n"
            f"Starting re-draft for {len(flagged_list)} flagged employees..."
        )
        await send_message(chat_id, summary)

        # Auto re-draft all flagged employees with properly extracted hooks
        if flagged_list:
            await _redraft_flagged(workers, chat_id)

    finally:
        _audit_running = False


class AgentEmailAuditRequest(BaseModel):
    workers: int = 5


@app.post("/agent/email/audit/batch")
async def agent_email_audit_batch(req: AgentEmailAuditRequest = None):
    """Audit all done email drafts — checks if research data (info) is used in the draft.
    Automatically re-drafts any flagged employees with hooks extracted from their info JSON."""
    global _audit_running
    if req is None:
        req = AgentEmailAuditRequest()
    if _audit_running:
        return JSONResponse(status_code=409, content={"ok": False, "error": "Audit already running"})

    workers = max(1, min(req.workers, 9))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) as cnt FROM employee WHERE draft_status = 'done'")
        row = await cur.fetchone()
        total = row["cnt"]

    if total == 0:
        return {"ok": True, "message": "No done employees to audit", "queued": 0}

    _audit_running = True
    asyncio.create_task(_run_email_audit(workers, ALLOWED_USER_ID))
    return {"ok": True, "queued": total, "workers": workers}


@app.get("/agent/email/audit/batch/status")
async def agent_email_audit_batch_status():
    """Live progress of the email audit batch."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT audit_status, COUNT(*) as cnt FROM employee GROUP BY audit_status"
        )
        rows = await cur.fetchall()
    counts = {(r["audit_status"] or "unaudited"): r["cnt"] for r in rows}
    checked = sum(v for k, v in counts.items() if k not in ("unaudited", "auditing", None))
    return {
        "ok":                   True,
        "audit_running":        _audit_running,
        "unaudited":            counts.get("unaudited", 0),
        "auditing":             counts.get("auditing", 0),
        "no_contact_info":      counts.get("no_contact_info", 0),
        "has_name_no_research": counts.get("has_name_no_research", 0),
        "real_info_used":       counts.get("real_info_used", 0),
        "real_info_unused":     counts.get("real_info_unused", 0),
        "checked":              checked,
    }


# -- Job scraper + research + pipeline orchestrator --------------------------

_pipeline_running: bool = False


async def _run_linkedin_scrape() -> dict:
    """Run Claude Code to scrape LinkedIn Jobs. Returns summary dict."""
    raw = await runner.run_query(_PROMPT_LINKEDIN_SCRAPER, timeout_secs=600)
    try:
        cleaned = raw.strip()
        si, ei = cleaned.find("{"), cleaned.rfind("}")
        if si != -1 and ei != -1:
            return json.loads(cleaned[si:ei + 1])
    except Exception as exc:
        logger.error("LinkedIn scrape parse error: %s", exc)
    return {"new_jobs_saved": 0, "duplicates_skipped": 0, "errors": 1, "jobs": []}


async def _run_jobs_research(workers: int = 3, chat_id: int = 0) -> dict:
    """Research hiring contacts for all New jobs using Claude Code web search."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT job_id, title, company, job_url, location, tech_stack, company_website "
            "FROM jobs WHERE status = 'New'"
        )
        rows = await cur.fetchall()
    jobs = list(rows)
    total = len(jobs)
    if total == 0:
        return {"contacted": 0, "not_found": 0, "total": 0}

    sem = asyncio.Semaphore(workers)
    contacted = 0
    not_found = 0

    async def _research_one(job) -> None:
        nonlocal contacted, not_found
        async with sem:
            try:
                company = job["company"] or ""
                domain = job["company_website"] or ""
                if not domain and company:
                    domain = company.lower().replace(" ", "") + ".com"
                prompt = _PROMPT_HIRING_MANAGER_RESEARCH.format(
                    company=company,
                    company_domain=domain,
                    title=job["title"] or "",
                    job_url=job["job_url"] or "",
                    location=job["location"] or "",
                    tech_stack=job["tech_stack"] or "",
                )
                raw = await runner.run_query(prompt, timeout_secs=300)
                cleaned = raw.strip()
                si, ei = cleaned.find("{"), cleaned.rfind("}")
                if si == -1 or ei == -1:
                    raise ValueError("No JSON in research response")
                result = json.loads(cleaned[si:ei + 1])

                contacts = result.get("all_contacts") or []
                primary_email = result.get("email", "")
                primary_name = result.get("name", "")
                primary_title = result.get("person_title", "")
                primary_linkedin = result.get("linkedin_url", "")
                confidence = result.get("confidence", "low")

                all_emails: list[dict] = []
                if primary_email:
                    all_emails.append({
                        "email": primary_email, "name": primary_name,
                        "title": primary_title, "linkedin_url": primary_linkedin,
                        "is_primary": True,
                    })
                for c in contacts:
                    e_addr = c.get("email", "")
                    if e_addr and e_addr != primary_email:
                        all_emails.append({
                            "email": e_addr, "name": c.get("name", ""),
                            "title": c.get("title", ""), "linkedin_url": c.get("linkedin_url", ""),
                            "is_primary": False,
                        })
                if not all_emails and primary_name:
                    placeholder = f"noemail_{job['job_id']}@placeholder.invalid"
                    all_emails.append({
                        "email": placeholder, "name": primary_name,
                        "title": primary_title, "linkedin_url": primary_linkedin,
                        "is_primary": True,
                    })

                async with aiosqlite.connect(DB_PATH) as db:
                    for contact in all_emails:
                        await db.execute(
                            "INSERT INTO employee (email, name, full_name, title, company, linkedin_url) "
                            "VALUES (?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(email) DO UPDATE SET "
                            "full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE full_name END, "
                            "linkedin_url = CASE WHEN excluded.linkedin_url != '' THEN excluded.linkedin_url ELSE linkedin_url END",
                            (contact["email"], contact["name"], contact["name"],
                             contact["title"], company, contact["linkedin_url"])
                        )
                        await db.execute(
                            "INSERT OR IGNORE INTO job_employee (job_id, email, is_primary) VALUES (?, ?, ?)",
                            (job["job_id"], contact["email"], 1 if contact["is_primary"] else 0)
                        )
                    new_status = "Ready to Email" if (all_emails and confidence != "low") else "Needs Manual Review"
                    alt_emails = json.dumps(result.get("alternative_emails") or [])
                    await db.execute(
                        "UPDATE jobs SET status = ?, alternative_emails = ? WHERE job_id = ?",
                        (new_status, alt_emails, job["job_id"])
                    )
                    await db.commit()

                if confidence != "low" and all_emails:
                    contacted += 1
                else:
                    not_found += 1

            except Exception as exc:
                logger.error("Job research error for %s: %s", job["job_id"], exc)
                not_found += 1

    await asyncio.gather(*[_research_one(j) for j in jobs])
    return {"contacted": contacted, "not_found": not_found, "total": total}


async def _run_pipeline(chat_id: int) -> None:
    """Full automated pipeline: scrape -> research -> draft -> audit.
    Workflow 3 picks up Ready-to-Email jobs on its own schedule."""
    global _pipeline_running
    try:
        await send_message(chat_id, "Step 1/4: Scraping LinkedIn Jobs...")
        scrape = await _run_linkedin_scrape()
        new_jobs = scrape.get("new_jobs_saved", 0)
        await send_message(
            chat_id,
            f"Step 1 done - {new_jobs} new jobs added ({scrape.get('duplicates_skipped', 0)} dupes skipped)"
        )

        await send_message(chat_id, "Step 2/4: Finding hiring managers...")
        research = await _run_jobs_research(workers=3, chat_id=chat_id)
        await send_message(
            chat_id,
            f"Step 2 done - {research['contacted']} jobs with contacts, "
            f"{research['not_found']} need manual review"
        )

        await send_message(chat_id, "Step 3/4: Drafting emails (9-agent orchestrator)...")
        await _run_email_batch(workers=3, chat_id=chat_id)

        await send_message(chat_id, "Step 4/4: Auditing email quality...")
        await _run_email_audit(workers=5, chat_id=chat_id)

        await send_message(
            chat_id,
            f"Pipeline complete!\n\nNew jobs: {new_jobs}\nContacts found: {research['contacted']}\n\n"
            "Workflow 3 sends emails at 9AM (PL) and 3PM Warsaw (US)."
        )
    except Exception as exc:
        logger.error("Pipeline error: %s", exc)
        await send_message(chat_id, f"Pipeline error: {exc}")
    finally:
        _pipeline_running = False


@app.post("/agent/jobs/scrape")
async def agent_jobs_scrape():
    """Scrape LinkedIn Jobs via Claude Code web search. Saves new jobs with status=New."""
    result = await _run_linkedin_scrape()
    return {"ok": True, **result}


@app.post("/agent/jobs/research")
async def agent_jobs_research():
    """Find hiring contacts for all New jobs via Claude Code web search. Free, no Perplexity."""
    result = await _run_jobs_research(workers=3)
    return {"ok": True, **result}


@app.post("/agent/pipeline/run")
async def agent_pipeline_run():
    """Run the full job search pipeline: scrape -> research -> draft -> audit.
    Workflow 3 handles sending on its own 9AM/3PM schedule."""
    global _pipeline_running
    if _pipeline_running:
        return JSONResponse(status_code=409, content={"ok": False, "error": "Pipeline already running"})
    _pipeline_running = True
    asyncio.create_task(_run_pipeline(ALLOWED_USER_ID))
    return {"ok": True, "message": "Pipeline started - check Telegram for progress"}


@app.get("/agent/pipeline/status")
async def agent_pipeline_status():
    """Check pipeline status and job counts by status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status")
        rows = await cur.fetchall()
    counts = {r["status"]: r["cnt"] for r in rows}
    return {
        "ok": True,
        "pipeline_running": _pipeline_running,
        "jobs_new": counts.get("New", 0),
        "jobs_ready_to_email": counts.get("Ready to Email", 0),
        "jobs_email_sent": counts.get("Email Sent", 0),
        "jobs_manual_review": counts.get("Needs Manual Review", 0),
    }

class HMInfoRequest(BaseModel):
    email: str
    name: str = ""
    title: str = ""
    company: str = ""
    linkedin_url: str = ""


@app.post("/agent/hm/info")
async def agent_hm_info(req: HMInfoRequest):
    """Research a hiring contact via Perplexity. Returns structured profile info
    to personalize outreach. Stores result in the employee table automatically."""
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        return JSONResponse(status_code=503, content={"ok": False, "error": "PERPLEXITY_API_KEY not configured"})

    linkedin_hint = f"\nLinkedIn URL: {req.linkedin_url}" if req.linkedin_url else ""
    prompt = PROMPTS["hm_info"].format(
        name=req.name,
        title=req.title,
        company=req.company,
        email=req.email,
        linkedin_hint=linkedin_hint,
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "sonar",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 800,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

        cleaned = content.replace("```json", "").replace("```", "").strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("No JSON in response")
        result = json.loads(cleaned[start:end + 1])

        # Auto-store in employee table
        info_text = json.dumps(result)
        confirmed_linkedin = result.get("linkedin_url") or req.linkedin_url
        confirmed_full_name = result.get("full_name") or req.name
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO employee (email, name, full_name, title, company, linkedin_url, info)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET
                     full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE full_name END,
                     linkedin_url = CASE WHEN excluded.linkedin_url != '' THEN excluded.linkedin_url ELSE linkedin_url END,
                     info = excluded.info""",
                (req.email, req.name, confirmed_full_name, req.title, req.company, confirmed_linkedin, info_text)
            )
            await db.commit()

        return {"ok": True, "email": req.email, **result}

    except Exception as exc:
        logger.error("HM info error: %s", exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


class JobIn(BaseModel):
    job_id: str
    title: str = ""
    company: str = ""
    job_url: str = ""
    location: str = ""
    salary: str = ""
    tech_stack: str = ""
    posted_date: str = ""
    source: str = ""
    status: str = "New"
    hiring_manager_email: str = ""
    email_sent_date: str = ""
    company_website: str = ""
    notes: str = ""
    fit_score: int = 0
    is_ai_agent_role: bool = False
    seniority: str = "mid"
    priority: str = "low"
    reason: str = ""


class JobUpdate(BaseModel):
    job_id: str
    status: Optional[str] = None
    hiring_manager_email: Optional[str] = None
    alternative_emails: Optional[str] = None
    company_website: Optional[str] = None
    notes: Optional[str] = None
    email_sent_date: Optional[str] = None


@app.get("/jobs")
async def get_jobs(status: Optional[str] = None):
    """Return all jobs from local SQLite DB. Optional ?status= filter (comma-separated for multiple)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            statuses = [s.strip() for s in status.split(',') if s.strip()]
            if len(statuses) == 1:
                cursor = await db.execute("SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC", (statuses[0],))
            else:
                placeholders = ','.join('?' * len(statuses))
                cursor = await db.execute(f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at DESC", statuses)
        else:
            cursor = await db.execute("SELECT * FROM jobs ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


@app.post("/jobs")
async def create_job(job: JobIn):
    """Insert a job into the DB. Silently ignores duplicates (same job_id or company+title).
    If the new job is 'Ready to Email' and shares a hiring_manager_email with an existing
    Ready to Email job, keeps only the highest fit_score one."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute(
                """INSERT OR IGNORE INTO jobs
                   (job_id, title, company, job_url, location, salary, tech_stack,
                    posted_date, source, status, hiring_manager_email, email_sent_date,
                    company_website, notes, fit_score, is_ai_agent_role, seniority, priority, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id, job.title, job.company, job.job_url, job.location,
                    job.salary, job.tech_stack, job.posted_date, job.source, job.status,
                    job.hiring_manager_email, job.email_sent_date, job.company_website,
                    job.notes, job.fit_score, int(job.is_ai_agent_role), job.seniority,
                    job.priority, job.reason,
                ),
            )
            await db.commit()
            inserted = db.total_changes > 0

            # Dedup: if new job is Ready to Email with a known email, resolve conflicts
            if inserted and job.status == "Ready to Email" and job.hiring_manager_email:
                email = job.hiring_manager_email.strip().lower()
                cur = await db.execute(
                    """SELECT job_id, fit_score FROM jobs
                       WHERE LOWER(TRIM(hiring_manager_email)) = ?
                         AND status = 'Ready to Email'
                       ORDER BY fit_score DESC""",
                    (email,)
                )
                dupes = await cur.fetchall()
                if len(dupes) > 1:
                    keep_id = dupes[0]["job_id"]
                    demote_ids = [d["job_id"] for d in dupes[1:]]
                    for dupe_id in demote_ids:
                        await db.execute(
                            "UPDATE jobs SET status = 'Duplicate - Skipped', "
                            "notes = notes || ' [Duplicate email — higher-priority job: ' || ? || ']' "
                            "WHERE job_id = ?",
                            (keep_id, dupe_id)
                        )
                    await db.commit()
                    logger.info(
                        "Email dedup (insert): kept %s, demoted %d duplicate(s) for %s",
                        keep_id, len(demote_ids), email
                    )

            return {"ok": True, "inserted": inserted}
        except Exception as exc:
            logger.error("DB insert error: %s", exc)
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.patch("/jobs")
async def update_job(update: JobUpdate):
    """Update status/email/notes on a job by job_id.
    When status is set to 'Ready to Email', automatically deduplicates by
    hiring_manager_email — only the highest fit_score job keeps that status."""
    fields, values = [], []
    if update.status is not None:
        fields.append("status = ?"); values.append(update.status)
    if update.hiring_manager_email is not None:
        fields.append("hiring_manager_email = ?"); values.append(update.hiring_manager_email)
    if update.alternative_emails is not None:
        fields.append("alternative_emails = ?"); values.append(update.alternative_emails)
    if update.company_website is not None:
        fields.append("company_website = ?"); values.append(update.company_website)
    if update.notes is not None:
        fields.append("notes = ?"); values.append(update.notes)
    if update.email_sent_date is not None:
        fields.append("email_sent_date = ?"); values.append(update.email_sent_date)
    if not fields:
        return JSONResponse(status_code=400, content={"ok": False, "error": "No fields to update"})
    values.append(update.job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?", values)
        await db.commit()

        # Dedup by hiring_manager_email whenever a job becomes "Ready to Email"
        # or when an email address is assigned to an already-ready job.
        should_dedup = (
            update.status == "Ready to Email" or
            (update.hiring_manager_email and update.status is None)
        )
        if should_dedup:
            # Get the current email + status for this job
            cur = await db.execute(
                "SELECT hiring_manager_email, status, fit_score FROM jobs WHERE job_id = ?",
                (update.job_id,)
            )
            row = await cur.fetchone()
            if row and row["hiring_manager_email"] and row["status"] == "Ready to Email":
                email = row["hiring_manager_email"].strip().lower()
                # Find all Ready to Email jobs with the same address
                cur2 = await db.execute(
                    """SELECT job_id, fit_score FROM jobs
                       WHERE LOWER(TRIM(hiring_manager_email)) = ?
                         AND status = 'Ready to Email'
                       ORDER BY fit_score DESC""",
                    (email,)
                )
                dupes = await cur2.fetchall()
                if len(dupes) > 1:
                    # Keep only the top fit_score; demote the rest
                    keep_id = dupes[0]["job_id"]
                    demote_ids = [d["job_id"] for d in dupes[1:]]
                    for dupe_id in demote_ids:
                        await db.execute(
                            "UPDATE jobs SET status = 'Duplicate - Skipped', "
                            "notes = notes || ' [Duplicate email — higher-priority job: ' || ? || ']' "
                            "WHERE job_id = ?",
                            (keep_id, dupe_id)
                        )
                    await db.commit()
                    logger.info(
                        "Email dedup: kept %s, demoted %d duplicate(s) for %s",
                        keep_id, len(demote_ids), email
                    )

        return {"ok": True}


class EmployeeIn(BaseModel):
    email: str
    name: str = ""
    full_name: str = ""
    title: str = ""
    company: str = ""
    linkedin_url: str = ""
    info: str = ""
    job_id: str = ""       # if provided, links this employee to a job
    is_primary: bool = False


class EmployeeUpdate(BaseModel):
    email: str
    name: Optional[str] = None
    full_name: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    linkedin_url: Optional[str] = None
    info: Optional[str] = None
    is_generic: Optional[int] = None   # 1 = generic inbox, 0 = named contact
    greeting: Optional[str] = None     # prepended by WF3 before sending
    email_draft: Optional[str] = None  # body of the email draft
    approved: Optional[int] = None     # NULL=unseen, 1=approved, 0=skipped


@app.get("/employees")
async def get_employees(job_id: Optional[str] = None, email: Optional[str] = None):
    """Return employees. Filter by ?job_id= (all contacts for a job) or ?email= (specific person)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if job_id:
            cursor = await db.execute(
                """SELECT e.*, je.is_primary FROM employee e
                   JOIN job_employee je ON e.email = je.email
                   WHERE je.job_id = ?
                   ORDER BY je.is_primary DESC""",
                (job_id,)
            )
        elif email:
            cursor = await db.execute("SELECT * FROM employee WHERE email = ?", (email,))
        else:
            cursor = await db.execute("SELECT * FROM employee ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("email_draft"):
                d["email_draft"] = _normalize_email_paragraphs(d["email_draft"])
            result.append(d)
        return result


@app.post("/employees")
async def create_employee(emp: EmployeeIn):
    """Upsert an employee and optionally link to a job."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO employee (email, name, full_name, title, company, linkedin_url, info)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                 name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                 full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE full_name END,
                 title = CASE WHEN excluded.title != '' THEN excluded.title ELSE title END,
                 company = CASE WHEN excluded.company != '' THEN excluded.company ELSE company END,
                 linkedin_url = CASE WHEN excluded.linkedin_url != '' THEN excluded.linkedin_url ELSE linkedin_url END,
                 info = CASE WHEN excluded.info != '' THEN excluded.info ELSE info END""",
            (emp.email, emp.name, emp.full_name, emp.title, emp.company, emp.linkedin_url, emp.info)
        )
        if emp.job_id:
            await db.execute(
                """INSERT OR REPLACE INTO job_employee (job_id, email, is_primary)
                   VALUES (?, ?, ?)""",
                (emp.job_id, emp.email, int(emp.is_primary))
            )
        await db.commit()
        return {"ok": True}


@app.patch("/employees")
async def update_employee(update: EmployeeUpdate):
    """Update linkedin_url, info, or other fields on an existing employee."""
    fields, values = [], []
    if update.name is not None:
        fields.append("name = ?"); values.append(update.name)
    if update.full_name is not None:
        fields.append("full_name = ?"); values.append(update.full_name)
    if update.title is not None:
        fields.append("title = ?"); values.append(update.title)
    if update.company is not None:
        fields.append("company = ?"); values.append(update.company)
    if update.linkedin_url is not None:
        fields.append("linkedin_url = ?"); values.append(update.linkedin_url)
    if update.info is not None:
        fields.append("info = ?"); values.append(update.info)
    if update.is_generic is not None:
        fields.append("is_generic = ?"); values.append(update.is_generic)
    if update.greeting is not None:
        fields.append("greeting = ?"); values.append(update.greeting)
    if update.email_draft is not None:
        fields.append("email_draft = ?"); values.append(update.email_draft)
    if update.approved is not None:
        fields.append("approved = ?"); values.append(update.approved)
    if not fields:
        return JSONResponse(status_code=400, content={"ok": False, "error": "No fields to update"})
    values.append(update.email)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE employee SET {', '.join(fields)} WHERE email = ?", values)
        await db.commit()
        return {"ok": True}


@app.get("/jobs/preflight")
async def jobs_preflight():
    """Scan the employee table for bad records and auto-fix them before email workflows run.

    Checks:
    - email_draft containing pipeline status phrases (agent meta-commentary leaked into draft)
    - name that is all-lowercase with no spaces (looks like a company handle / slug)
    - name containing '@' (email address stored in name field)
    - name with 4+ words (too long to be a real first name)

    Fixes:
    - Bad email_draft → clear to '', set draft_status = 'pending'
    - Bad name → clear to ''
    - Sets audit_status = 'auto_fixed' and audit_notes = '<reason>'

    Returns: {"checked": N, "fixed": M, "issues": [...]}
    """
    # Use module-level _PIPELINE_TELLS so the list stays in sync with _is_pipeline_status
    issues = []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT email, name, email_draft, draft_status FROM employee")
        rows = await cursor.fetchall()
        checked = len(rows)

        for row in rows:
            email = row["email"]
            name = row["name"] or ""
            draft = row["email_draft"] or ""

            fix_draft = False
            fix_name = False
            reasons = []

            # Check email_draft for pipeline status phrases using module-level _is_pipeline_status
            if _is_pipeline_status(draft):
                fix_draft = True
                # Find and report the specific phrase that triggered it
                triggered = next((p for p in _PIPELINE_TELLS if p.lower() in draft.lower()), "starts with non-email text")
                reasons.append(f"email_draft contains pipeline phrase: '{triggered}'")

            # Check name: all-lowercase no-spaces (company handle)
            if name and name == name.lower() and " " not in name:
                fix_name = True
                reasons.append(f"name looks like company handle (all-lowercase no-spaces): '{name}'")

            # Check name: contains '@'
            if "@" in name:
                fix_name = True
                reasons.append(f"name contains '@': '{name}'")

            # Check name: 4+ words
            if name and len(name.split()) >= 4:
                fix_name = True
                reasons.append(f"name has 4+ words: '{name}'")

            if fix_draft or fix_name:
                audit_note = "; ".join(reasons)[:500]
                fields, values = [], []

                if fix_draft:
                    fields.append("email_draft = ''")
                    fields.append("draft_status = 'pending'")

                if fix_name:
                    fields.append("name = ''")

                fields.append("audit_status = 'auto_fixed'")
                fields.append(f"audit_notes = ?")
                values.append(audit_note)
                values.append(email)

                await db.execute(
                    f"UPDATE employee SET {', '.join(fields)} WHERE email = ?",
                    values,
                )

                issues.append({"email": email, "reasons": reasons})

        if issues:
            await db.commit()

    fixed = len(issues)
    logger.info("Pre-flight audit: checked=%d fixed=%d", checked, fixed)
    return {"checked": checked, "fixed": fixed, "issues": issues}


@app.get("/prompts")
async def get_prompts(name: Optional[str] = None):
    """Return all prompts from the PROMPTS dictionary, or a single prompt by ?name=key."""
    if name:
        if name not in PROMPTS:
            return JSONResponse(status_code=404, content={"error": f"Prompt '{name}' not found", "available": list(PROMPTS.keys())})
        return {"name": name, "prompt": PROMPTS[name]}
    return {"prompts": {k: {"length": len(v), "preview": v[:120] + "..."} for k, v in PROMPTS.items()}}


@app.get("/resume")
async def get_resume():
    """Serve the user's resume PDF for email attachments."""
    if not RESUME_PATH or not os.path.exists(RESUME_PATH):
        return JSONResponse(status_code=404, content={"error": "Resume not found. Set RESUME_PATH in .env"})
    filename = os.path.basename(RESUME_PATH)
    return FileResponse(RESUME_PATH, media_type="application/pdf", filename=filename)


@app.get("/review/queue")
async def review_queue():
    """Return all reviewable emails (draft_status=done) joined with job data, unapproved first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT
                e.email, e.full_name, e.name, e.email_draft, e.greeting,
                e.is_generic, e.approved, e.draft_status, e.audit_status,
                e.title AS contact_title,
                j.job_id, j.title, j.company, j.job_url, j.fit_score,
                j.hiring_manager_email, j.location, j.tech_stack,
                j.salary, j.notes, j.reason,
                je.is_primary
            FROM employee e
            JOIN job_employee je ON e.email = je.email
            JOIN jobs j ON je.job_id = j.job_id
            WHERE e.draft_status = 'done' AND e.email_draft != ''
            ORDER BY
                CASE WHEN e.approved IS NULL THEN 0 ELSE 1 END ASC,
                j.fit_score DESC,
                j.company ASC
        """)
        rows = await cursor.fetchall()

    results = []
    for row in rows:
        r = dict(row)
        # Compute greeting if not already set
        if not r.get("greeting"):
            full = (r.get("full_name") or r.get("name") or "").strip()
            is_generic = r.get("is_generic")
            company = r.get("company") or "the company"
            if full and is_generic != 1:
                first = full.split()[0]
                r["computed_greeting"] = f"Hey {first},"
            else:
                r["computed_greeting"] = f"Dear Hiring Team at {company},"
        else:
            r["computed_greeting"] = r["greeting"]
        results.append(r)

    total = len(results)
    approved = sum(1 for r in results if r.get("approved") == 1)
    skipped = sum(1 for r in results if r.get("approved") == 0)
    unseen = sum(1 for r in results if r.get("approved") is None)
    return {"items": results, "total": total, "approved": approved, "skipped": skipped, "unseen": unseen}


@app.post("/review/improve")
async def review_improve(body: dict):
    """Call Claude Sonnet to polish or regenerate an email draft with full context."""
    mode = body.get("mode", "polish")  # 'polish' or 'regenerate'
    current_draft = (body.get("draft") or "").strip()
    instructions = (body.get("instructions") or "").strip()
    job = body.get("job", {})
    employee = body.get("employee", {})

    # Job fields
    title = job.get("title", "the position")
    company = job.get("company", "the company")
    tech = job.get("tech_stack", "")
    job_url = job.get("job_url", "")
    location = job.get("location", "")
    salary = job.get("salary", "")
    notes = job.get("notes", "")
    reason = job.get("reason", "")

    # Employee fields
    name = (employee.get("full_name") or employee.get("name") or "").strip()
    emp_title = employee.get("title", "")

    instr_block = f"\nAdditional instructions (highest priority — follow these first): {instructions}" if instructions else ""

    # Load ChromaDB memory context
    memory_ctx = ""
    try:
        memory_ctx = await memory_handler.search_memory(f"{title} {company} {tech} email outreach cold email job application")
    except Exception as e:
        logger.warning("Memory search failed in review/improve: %s", e)

    # Read key context files from MEMORY_DIR
    jefe_ctx = ""
    for fname in ["USER.md", "MEMORY.md", "REMEMBERED.md", "PROJECTS.md"]:
        fpath = os.path.join(MEMORY_DIR, fname)
        try:
            if os.path.exists(fpath):
                with open(fpath) as f:
                    jefe_ctx += f"\n\n--- {fname} ---\n{f.read(4000)}"
        except Exception:
            pass

    # Also try to read the resume PDF as text if pdfminer/pdfplumber available, else skip
    resume_ctx = ""
    resume_path = RESUME_PATH
    try:
        import pdfplumber
        with pdfplumber.open(resume_path) as pdf:
            resume_ctx = "\n".join(p.extract_text() or "" for p in pdf.pages)[:6000]
    except Exception:
        pass

    context_block = ""
    if jefe_ctx:
        context_block += f"\n\n=== SENDER CONTEXT (USER.md / MEMORY / PROJECTS / REMEMBERED) ===\n{jefe_ctx}"
    if resume_ctx:
        context_block += f"\n\n=== RESUME (full text) ===\n{resume_ctx}"
    if memory_ctx:
        context_block += f"\n\n=== MEMORY / RELEVANT NOTES ===\n{memory_ctx}"
    context_block += f"\n\n=== FILES AVAILABLE FOR DEEPER RESEARCH ===\nYou have Read, Glob, Grep tools. Key paths:\n- {MEMORY_DIR}/ — USER.md, MEMORY.md, PROJECTS.md, REMEMBERED.md\n- {MEMORY_DIR}/Jobs/ — resume, cover letters, job plan\n- {MEMORY_DIR}/Projects/ — all active projects\nUse them if you need more specific details to tailor this email."

    job_block = f"""=== JOB DETAILS ===
Role: {title} at {company}
Location: {location}
Tech stack: {tech}
Salary: {salary}
Job URL: {job_url}
Notes: {notes}
Why applying: {reason}
Contact: {f"{name} ({emp_title})" if name else "hiring team inbox"}"""

    if mode == "polish":
        prompt = f"""You are editing a cold outreach email for a job application. Polish the email body below.
{context_block}

{job_block}

Rules:
- No dashes or double-dashes (— or --). Rewrite sentences instead.
- No AI-sounding phrases like "I am excited to", "passionate about", "I would love to", "leverage", "synergy"
- Keep it under 5 sentences total in the body
- Sound like a real person wrote it
- No greeting line or signature — body only
- Preserve real specific facts — do not invent new ones
- End with a short call to action like "Do you have 15 minutes for a quick call?"
- Return ONLY the improved body text, nothing else{instr_block}

Current draft:
{current_draft}"""
    else:
        prompt = f"""Write a cold outreach email body for a job application. You have full context about the sender below — use it to make this genuinely tailored, specific, and compelling. Not generic.
{context_block}

{job_block}

Rules:
- No greeting line, no signature — body only
- 3-4 short punchy paragraphs
- No dashes or double-dashes (— or --). Use commas or periods instead.
- No AI-sounding phrases like "excited to", "passionate about", "leverage", "synergy"
- Sound like a real human wrote it
- Pull specific real accomplishments and projects from the context above — tailor to this exact role and company
- End with: "Do you have 15 minutes for a quick call?"
- Return ONLY the body text, nothing else{instr_block}

Current draft (for style reference):
{current_draft}"""

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--model", "claude-sonnet-4-6",
            "--output-format", "text",
            "--allowedTools", "Read,Glob,Grep",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        improved = stdout.decode().strip()
        if not improved:
            improved = current_draft
        return {"ok": True, "draft": improved}
    except Exception as e:
        logger.error("review/improve error: %s", e)
        return {"ok": False, "draft": current_draft, "error": str(e)}


@app.get("/review", response_class=HTMLResponse)
async def get_review():
    """Human-in-the-loop email review interface."""
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email Review</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;flex-direction:column}
.topbar{background:#1e293b;border-bottom:1px solid #334155;padding:14px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.topbar h1{color:#818cf8;font-size:18px;font-weight:700;flex-shrink:0}
.stats{display:flex;gap:12px;flex-wrap:wrap}
.stat{font-size:12px;padding:3px 10px;border-radius:99px;font-weight:600}
.stat.approved{background:#14532d;color:#4ade80}
.stat.skipped{background:#450a0a;color:#f87171}
.stat.unseen{background:#1e293b;color:#94a3b8;border:1px solid #334155}
.stat.total{background:#1e1b4b;color:#818cf8}
.progress-wrap{flex:1;min-width:120px;max-width:300px}
.progress-bar{height:6px;background:#1e293b;border-radius:3px;overflow:hidden;margin-top:4px}
.progress-fill{height:100%;background:linear-gradient(90deg,#6366f1,#818cf8);border-radius:3px;transition:width .3s}
.progress-label{font-size:11px;color:#64748b}
.shortcuts{margin-left:auto;font-size:11px;color:#475569}
.shortcuts kbd{background:#1e293b;border:1px solid #334155;border-radius:3px;padding:1px 5px;font-size:10px;color:#94a3b8}
.main{flex:1;padding:20px;max-width:780px;width:100%;margin:0 auto}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;overflow:hidden;margin-bottom:16px}
.card-header{padding:14px 18px;border-bottom:1px solid #334155;display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
.card-meta{display:flex;flex-direction:column;gap:4px}
.company-role{font-size:16px;font-weight:600;color:#e2e8f0}
.to-line{font-size:12px;color:#64748b}
.to-line a{color:#818cf8;text-decoration:none}
.badges{display:flex;gap:6px;align-items:center;flex-shrink:0}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;font-weight:600}
.badge.fit{background:#1e1b4b;color:#818cf8}
.badge.approved{background:#14532d;color:#4ade80}
.badge.skipped{background:#450a0a;color:#f87171}
.badge.unseen{background:#0f172a;color:#64748b;border:1px solid #334155}
.email-wrap{padding:18px}
.greeting-row{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.greeting-text{font-size:15px;color:#e2e8f0;font-weight:500}
.edit-greeting-btn{font-size:11px;color:#64748b;cursor:pointer;padding:2px 6px;border-radius:4px;border:1px solid #334155;background:transparent}
.edit-greeting-btn:hover{color:#94a3b8;border-color:#475569}
.greeting-input{display:none;width:100%;background:#0f172a;border:1px solid #6366f1;border-radius:6px;color:#e2e8f0;padding:6px 10px;font-size:14px;margin-bottom:12px}
.email-preview{white-space:pre-wrap;font-size:14px;line-height:1.7;color:#cbd5e1;cursor:pointer;padding:14px;background:#0f172a;border-radius:8px;border:1px solid #1e293b;transition:border-color .2s;min-height:120px}
.email-preview:hover{border-color:#334155}
.email-preview .signature{color:#475569;margin-top:12px}
.email-textarea{display:none;width:100%;background:#0f172a;border:1px solid #6366f1;border-radius:8px;color:#e2e8f0;padding:14px;font-size:14px;line-height:1.7;resize:vertical;min-height:200px;font-family:inherit}
.email-textarea:focus{outline:none;border-color:#818cf8}
.edit-hint{font-size:11px;color:#475569;margin-top:6px}
.signature-block{margin-top:14px;padding:10px 14px;background:#0a0f1e;border-radius:6px;border:1px solid #1e293b;font-size:13px;color:#475569;white-space:pre-wrap;word-break:break-all;line-height:1.6}
.actions{padding:14px 18px;border-top:1px solid #1e293b;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn{padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:all .15s;display:inline-flex;align-items:center;gap:5px}
.btn:disabled{opacity:0.4;cursor:not-allowed}
.btn-approve{background:#16a34a;color:#fff}.btn-approve:hover:not(:disabled){background:#15803d}
.btn-skip{background:#334155;color:#94a3b8}.btn-skip:hover:not(:disabled){background:#475569}
.btn-polish{background:#1d4ed8;color:#fff}.btn-polish:hover:not(:disabled){background:#1e40af}
.btn-regen{background:#6d28d9;color:#fff}.btn-regen:hover:not(:disabled){background:#5b21b6}
.btn-save{background:#0e7490;color:#fff}.btn-save:hover:not(:disabled){background:#0c6384}
.btn-cancel{background:#1e293b;color:#64748b;border:1px solid #334155}.btn-cancel:hover:not(:disabled){background:#334155}
.edit-actions{display:none;gap:8px}
.nav-actions{margin-left:auto;display:flex;gap:6px}
.btn-nav{background:#1e293b;color:#64748b;border:1px solid #334155;padding:6px 14px;font-size:12px}.btn-nav:hover:not(:disabled){background:#334155;color:#94a3b8}
.regen-wrap{display:flex;gap:6px;align-items:center;flex:1;min-width:160px}
.regen-input{background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:12px;padding:6px 10px;flex:1;outline:none;min-width:0;font-family:inherit}.regen-input:focus{border-color:#7c3aed}.regen-input::placeholder{color:#475569}
.improve-result{display:none;margin:12px 18px;background:#0a1628;border:1px solid #334155;border-radius:8px;overflow:hidden}
.improve-header{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#1e293b;font-size:12px;color:#94a3b8}
.improve-body{padding:12px;font-size:13px;color:#cbd5e1;white-space:pre-wrap;line-height:1.6;max-height:300px;overflow-y:auto}
.improve-actions{padding:8px 12px;border-top:1px solid #1e293b;display:flex;gap:6px}
.btn-use{background:#16a34a;color:#fff;padding:5px 12px;font-size:12px}.btn-use:hover{background:#15803d}
.btn-discard{background:#334155;color:#94a3b8;padding:5px 12px;font-size:12px;border:none}.btn-discard:hover{background:#475569}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-overlay{position:absolute;inset:0;background:rgba(15,23,42,.7);display:flex;align-items:center;justify-content:center;border-radius:8px;font-size:13px;color:#94a3b8;gap:8px}
.email-wrap{position:relative}
.empty-state{text-align:center;padding:60px 20px;color:#475569}
.empty-state h2{color:#64748b;margin-bottom:8px;font-size:20px}
.empty-state p{font-size:14px}
.all-done{text-align:center;padding:60px 20px}
.all-done h2{color:#4ade80;font-size:24px;margin-bottom:8px}
.all-done p{color:#64748b;font-size:14px}
</style>
</head>
<body>
<div class="topbar">
  <h1>📧 Email Review</h1>
  <div class="stats">
    <span class="stat total" id="stat-total">— total</span>
    <span class="stat approved" id="stat-approved">— ✓</span>
    <span class="stat skipped" id="stat-skipped">— ✗</span>
    <span class="stat unseen" id="stat-unseen">— remaining</span>
  </div>
  <div class="progress-wrap">
    <div class="progress-label" id="prog-label">Loading...</div>
    <div class="progress-bar"><div class="progress-fill" id="prog-fill" style="width:0%"></div></div>
  </div>
  <div class="shortcuts">
    <kbd>A</kbd> approve &nbsp;
    <kbd>S</kbd> skip &nbsp;
    <kbd>E</kbd> edit &nbsp;
    <kbd>P</kbd> polish &nbsp;
    <kbd>R</kbd> regen &nbsp;
    <kbd>←</kbd><kbd>→</kbd> navigate
  </div>
</div>
<div class="main" id="main">
  <div class="empty-state"><div class="spinner"></div></div>
</div>

<script>
const SIGNATURE = `${window.EMAIL_SIGNATURE || 'Best regards'}`;
const SERVER = '';

let queue = [];
let idx = 0;
let editMode = false;
let improveResult = null;

function getIdx() {
  const p = new URLSearchParams(location.search);
  return parseInt(p.get('idx') || localStorage.getItem('review_idx') || '0', 10);
}
function saveIdx(i) {
  const p = new URLSearchParams(location.search);
  p.set('idx', i);
  history.replaceState(null, '', '?' + p.toString());
  localStorage.setItem('review_idx', i);
}

async function loadQueue() {
  const res = await fetch(SERVER + '/review/queue');
  const data = await res.json();
  queue = data.items || [];
  updateStats(data);
  idx = Math.min(getIdx(), queue.length - 1);
  if (idx < 0) idx = 0;
  render();
}

function updateStats(data) {
  document.getElementById('stat-total').textContent = (data.total || 0) + ' total';
  document.getElementById('stat-approved').textContent = (data.approved || 0) + ' ✓';
  document.getElementById('stat-skipped').textContent = (data.skipped || 0) + ' ✗';
  document.getElementById('stat-unseen').textContent = (data.unseen || 0) + ' remaining';
  const pct = data.total ? Math.round(((data.approved + data.skipped) / data.total) * 100) : 0;
  document.getElementById('prog-fill').style.width = pct + '%';
  document.getElementById('prog-label').textContent = `${data.approved + data.skipped} of ${data.total} reviewed (${pct}%)`;
}

function assembleEmail(item) {
  const greeting = item.computed_greeting || item.greeting || '';
  const body = (item.email_draft || '').trim();
  const refLine = item.job_url ? `\nReferencing: ${item.job_url}` : '';
  return { greeting, body, refLine };
}

function render() {
  const main = document.getElementById('main');
  if (!queue.length) {
    main.innerHTML = '<div class="empty-state"><h2>No emails to review</h2><p>Emails with draft_status=done will appear here.</p></div>';
    return;
  }
  if (idx >= queue.length) {
    main.innerHTML = '<div class="all-done"><h2>✓ All done!</h2><p>All emails have been reviewed. Go to workflow 3 to send approved emails.</p></div>';
    return;
  }

  const item = queue[idx];
  const { greeting, body, refLine } = assembleEmail(item);
  const statusBadge = item.approved === 1
    ? '<span class="badge approved">✓ Approved</span>'
    : item.approved === 0
    ? '<span class="badge skipped">✗ Skipped</span>'
    : '<span class="badge unseen">Unseen</span>';

  main.innerHTML = `
  <div class="card">
    <div class="card-header">
      <div class="card-meta">
        <div class="company-role">${esc(item.company)} &mdash; ${esc(item.title)}</div>
        <div class="to-line">To: <a href="mailto:${esc(item.email)}">${esc(item.email)}</a> &nbsp;|&nbsp; ${idx+1} of ${queue.length}</div>
      </div>
      <div class="badges">
        ${item.fit_score ? `<span class="badge fit">⭐ ${item.fit_score}</span>` : ''}
        ${statusBadge}
      </div>
    </div>

    <div class="email-wrap" id="email-wrap">
      <div class="greeting-row">
        <span class="greeting-text" id="greeting-display">${esc(greeting)}</span>
        <button class="edit-greeting-btn" onclick="toggleGreetingEdit()">✏ greeting</button>
      </div>
      <input class="greeting-input" id="greeting-input" value="${esc(greeting)}" placeholder="Hey Name," />

      <div class="email-preview" id="email-preview" onclick="enterEdit()">${esc(body)}</div>
      <textarea class="email-textarea" id="email-textarea" rows="12">${esc(body)}</textarea>
      <div class="edit-hint" id="edit-hint">Click body to edit &nbsp;·&nbsp; <kbd>E</kbd> to edit</div>

      <div class="signature-block">${esc(refLine ? refLine + '\n\n' + SIGNATURE : '\n' + SIGNATURE)}</div>
    </div>

    <div class="improve-result" id="improve-result">
      <div class="improve-header">
        <span id="improve-label">AI suggestion</span>
        <span id="improve-mode-badge"></span>
      </div>
      <div class="improve-body" id="improve-body"></div>
      <div class="improve-actions">
        <button class="btn btn-use" onclick="useImproved()">Use this</button>
        <button class="btn btn-discard" onclick="discardImproved()">Discard</button>
      </div>
    </div>

    <div class="actions">
      <button class="btn btn-approve" id="btn-approve" onclick="approve()">✓ Approve</button>
      <button class="btn btn-skip" id="btn-skip" onclick="skip()">✗ Skip</button>
      <button class="btn btn-polish" id="btn-polish" onclick="improve('polish')">✨ Polish</button>
      <div class="regen-wrap">
        <input class="regen-input" id="regen-input" type="text" placeholder="regen instructions, e.g. make it shorter…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();improve('regenerate')}">
        <button class="btn btn-regen" id="btn-regen" onclick="improve('regenerate')">🔄 Regen</button>
      </div>
      <div class="edit-actions" id="edit-actions">
        <button class="btn btn-save" onclick="saveEdit()">💾 Save</button>
        <button class="btn btn-cancel" onclick="cancelEdit()">Cancel</button>
      </div>
      <div class="nav-actions">
        <button class="btn btn-nav" onclick="navigate(-1)" ${idx===0?'disabled':''}>← Prev</button>
        <button class="btn btn-nav" onclick="navigate(1)" ${idx>=queue.length-1?'disabled':''}>Next →</button>
      </div>
    </div>
  </div>`;

  editMode = false;
  saveIdx(idx);
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function enterEdit() {
  if (editMode) return;
  editMode = true;
  document.getElementById('email-preview').style.display = 'none';
  document.getElementById('email-textarea').style.display = 'block';
  document.getElementById('email-textarea').focus();
  document.getElementById('edit-hint').style.display = 'none';
  document.getElementById('edit-actions').style.display = 'flex';
}

function cancelEdit() {
  editMode = false;
  document.getElementById('email-preview').style.display = 'block';
  document.getElementById('email-textarea').style.display = 'none';
  document.getElementById('edit-hint').style.display = 'block';
  document.getElementById('edit-actions').style.display = 'none';
  // restore original
  document.getElementById('email-textarea').value = queue[idx].email_draft || '';
}

async function saveEdit() {
  const newDraft = document.getElementById('email-textarea').value;
  const newGreeting = document.getElementById('greeting-input').value || queue[idx].computed_greeting;
  const email = queue[idx].email;
  await fetch(SERVER + '/employees', {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email, email_draft: newDraft, greeting: newGreeting})
  });
  queue[idx].email_draft = newDraft;
  queue[idx].greeting = newGreeting;
  queue[idx].computed_greeting = newGreeting;
  render();
}

function toggleGreetingEdit() {
  const inp = document.getElementById('greeting-input');
  const disp = document.getElementById('greeting-display');
  if (inp.style.display === 'none' || !inp.style.display) {
    inp.style.display = 'block';
    disp.style.display = 'none';
    inp.focus();
  } else {
    inp.style.display = 'none';
    disp.style.display = 'inline';
    disp.textContent = inp.value;
  }
}

async function approve() {
  const email = queue[idx].email;
  const greeting = (document.getElementById('greeting-input')?.value || queue[idx].computed_greeting || '').trim();
  // Optimistic update — navigate immediately, sync in background
  queue[idx].approved = 1;
  updateStatsLocal();
  navigate(1);
  fetch(SERVER + '/employees', {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email, approved: 1, greeting})
  }).then(() => refreshStats()).catch(() => {});
}

async function skip() {
  const email = queue[idx].email;
  // Optimistic update — navigate immediately, sync in background
  queue[idx].approved = 0;
  updateStatsLocal();
  navigate(1);
  fetch(SERVER + '/employees', {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email, approved: 0})
  }).then(() => refreshStats()).catch(() => {});
}

function updateStatsLocal() {
  const total = queue.length;
  const approved = queue.filter(i => i.approved === 1).length;
  const skipped = queue.filter(i => i.approved === 0).length;
  const unseen = queue.filter(i => i.approved === null || i.approved === undefined).length;
  const pct = total ? Math.round(((approved + skipped) / total) * 100) : 0;
  document.getElementById('stat-total').textContent = total + ' total';
  document.getElementById('stat-approved').textContent = approved + ' ✓';
  document.getElementById('stat-skipped').textContent = skipped + ' ✗';
  document.getElementById('stat-unseen').textContent = unseen + ' remaining';
  document.getElementById('prog-fill').style.width = pct + '%';
  document.getElementById('prog-label').textContent = `${approved + skipped} of ${total} reviewed (${pct}%)`;
}

async function refreshStats() {
  const res = await fetch(SERVER + '/review/queue');
  const data = await res.json();
  queue = data.items || [];
  updateStats(data);
}

function navigate(dir) {
  improveResult = null;
  idx = Math.max(0, Math.min(queue.length - 1, idx + dir));
  render();
  window.scrollTo(0, 0);
}

async function improve(mode) {
  const item = queue[idx];
  const btnId = mode === 'polish' ? 'btn-polish' : 'btn-regen';
  const btn = document.getElementById(btnId);
  if (!btn) return;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>';
  btn.disabled = true;

  const regenInput = document.getElementById('regen-input');
  const instructions = regenInput ? regenInput.value.trim() : '';

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 180000);

  try {
    const res = await fetch(SERVER + '/review/improve', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      signal: controller.signal,
      body: JSON.stringify({
        instructions,
        mode,
        draft: item.email_draft,
        job: {title: item.title, company: item.company, job_url: item.job_url, tech_stack: item.tech_stack, location: item.location, salary: item.salary, notes: item.notes, reason: item.reason},
        employee: {full_name: item.full_name, name: item.name, title: item.contact_title}
      })
    });
    const data = await res.json();
    if (data.ok && data.draft) {
      improveResult = data.draft;
      document.getElementById('improve-result').style.display = 'block';
      document.getElementById('improve-body').textContent = data.draft;
      document.getElementById('improve-label').textContent = mode === 'polish' ? '✨ Polished version' : '🔄 Regenerated version';
    } else if (data.error) {
      alert('Error: ' + data.error);
    }
  } catch (err) {
    const msg = err.name === 'AbortError' ? 'Request timed out (90s). Try again.' : 'Request failed: ' + err.message;
    alert(msg);
  } finally {
    clearTimeout(timer);
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

async function useImproved() {
  if (!improveResult) return;
  queue[idx].email_draft = improveResult;
  document.getElementById('improve-result').style.display = 'none';
  improveResult = null;
  // Auto-save to DB
  const email = queue[idx].email;
  await fetch(SERVER + '/employees', {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email, email_draft: queue[idx].email_draft})
  });
  render();
  enterEdit();
  document.getElementById('email-textarea').value = queue[idx].email_draft;
}

function discardImproved() {
  improveResult = null;
  document.getElementById('improve-result').style.display = 'none';
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (['INPUT','TEXTAREA'].includes(e.target.tagName)) {
    if (e.key === 'Escape') cancelEdit();
    if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); saveEdit(); }
    return;
  }
  switch(e.key) {
    case 'a': case 'A': approve(); break;
    case 's': case 'S': skip(); break;
    case 'e': case 'E': enterEdit(); break;
    case 'p': case 'P': improve('polish'); break;
    case 'r': case 'R': improve('regenerate'); break;
    case 'ArrowLeft': navigate(-1); break;
    case 'ArrowRight': navigate(1); break;
    case 'Escape': cancelEdit(); break;
  }
});

loadQueue();
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    """HTML job visualization dashboard with Jobs + Employees tabs."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Search Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;padding:24px}
  h1{color:#818cf8;font-size:22px;margin-bottom:8px}
  .header-row{display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap}
  .refresh-info{font-size:12px;color:#475569}
  #last-updated{color:#94a3b8}
  #refresh-btn{background:#1e293b;border:1px solid #334155;color:#94a3b8;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}
  #refresh-btn:hover{background:#273548;color:#e2e8f0}
  #refresh-btn.spinning{opacity:0.6;cursor:default}
  .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #1e293b;padding-bottom:0}
  .tab{padding:8px 20px;cursor:pointer;font-size:13px;color:#64748b;border-radius:8px 8px 0 0;border:1px solid transparent;border-bottom:none;margin-bottom:-1px}
  .tab.active{background:#1e293b;color:#818cf8;border-color:#334155;border-bottom-color:#0f172a}
  .tab:hover:not(.active){color:#94a3b8}
  .pane{display:none}.pane.active{display:block}
  .chips{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}
  .chip{background:#1e293b;padding:12px 18px;border-radius:10px;font-size:13px;color:#94a3b8}
  .chip-n{display:block;font-size:26px;font-weight:700;color:#818cf8;line-height:1.1}
  .search-bar{margin-bottom:14px}
  .search-bar input{background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:7px 12px;border-radius:7px;font-size:13px;width:280px}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th{background:#1e293b;padding:9px 10px;text-align:left;color:#64748b;font-weight:600;white-space:nowrap}
  td{padding:8px 10px;border-bottom:1px solid #1e293b;vertical-align:top;max-width:320px;overflow-wrap:break-word}
  tr:hover td{background:#1a2436}
  a{color:#818cf8;text-decoration:none}a:hover{text-decoration:underline}
  .status{display:inline-block;padding:2px 7px;border-radius:5px;font-size:11px;font-weight:600}
  .status-new{background:#1e3a5f;color:#60a5fa}
  .status-ready-for-email-search{background:#1a3a2a;color:#4ade80}
  .status-ready-to-email{background:#2a1a3a;color:#c084fc}
  .status-email-sent{background:#3a2a1a;color:#fb923c}
  .status-needs-manual-review{background:#3a1a1a;color:#f87171}
  .info-cell{font-size:11px;color:#64748b;max-width:260px}
  .has-info{color:#4ade80;font-weight:600}
  .audit-badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600}
  .audit-real-used{background:#14532d;color:#4ade80}
  .audit-real-unused{background:#7f1d1d;color:#f87171}
  .audit-name-only{background:#1c1917;color:#a8a29e}
  .audit-no-info{background:#0f172a;color:#475569}
  .audit-auditing{background:#1e1b4b;color:#a5b4fc}
  .filter-row{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
  .filter-btn{background:#1e293b;border:1px solid #334155;color:#64748b;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px}
  .filter-btn.active{background:#312e81;border-color:#4f46e5;color:#a5b4fc}
  .hook-preview{font-size:11px;color:#94a3b8;font-style:italic;margin-top:3px}
</style>
</head>
<body>
<div class="header-row">
  <h1 id="title">Job Search Dashboard</h1>
  <span class="refresh-info">Auto-refresh: 15s &nbsp;|&nbsp; Last updated: <span id="last-updated">loading...</span></span>
  <button id="refresh-btn" onclick="loadAll()">&#x21bb; Refresh</button>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('jobs')">Jobs</div>
  <div class="tab" onclick="switchTab('employees')">Employees</div>
</div>

<!-- JOBS PANE -->
<div id="pane-jobs" class="pane active">
<div class="chips" id="chips"></div>
<table>
<thead><tr>
  <th>Title</th><th>Company</th><th>Location</th>
  <th>Score</th><th>Priority</th><th>Status</th>
  <th>Email</th><th>Alt Emails</th><th>Source</th><th>Date</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>

<!-- EMPLOYEES PANE -->
<div id="pane-employees" class="pane">
<div class="chips" id="emp-chips"></div>
<div class="filter-row">
  <input id="emp-search" type="text" placeholder="Filter by name, company, email..." oninput="filterEmployees()" style="background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:7px 12px;border-radius:7px;font-size:13px;width:260px">
  <span style="font-size:12px;color:#475569">Status:</span>
  <button class="filter-btn active" onclick="setDraftFilter('all', this)">All</button>
  <button class="filter-btn" onclick="setDraftFilter('done', this)">Done</button>
  <button class="filter-btn" onclick="setDraftFilter('pending', this)">Pending</button>
  <span style="font-size:12px;color:#475569;margin-left:8px">Audit:</span>
  <button class="filter-btn" onclick="setAuditFilter('all', this)">All</button>
  <button class="filter-btn" onclick="setAuditFilter('real_info_unused', this)">Flagged</button>
  <button class="filter-btn" onclick="setAuditFilter('real_info_used', this)">Info Used</button>
  <button class="filter-btn" onclick="setAuditFilter('has_name_no_research', this)">Name Only</button>
</div>
<table>
<thead><tr>
  <th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>LinkedIn</th><th>Info / Hook</th><th>Audit</th><th>Draft</th><th>Added</th>
</tr></thead>
<tbody id="emp-tbody"></tbody>
</table>
</div>

<script>
let _allEmployees = [];
let _activeTab = 'jobs';
let _draftFilter = 'all';
let _auditFilter = 'all';

function switchTab(name) {
  _activeTab = name;
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['jobs','employees'][i] === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-' + name).classList.add('active');
}

function setDraftFilter(val, btn) {
  _draftFilter = val;
  btn.closest('.filter-row').querySelectorAll('.filter-btn').forEach(b => {
    if (b.textContent !== 'All' && !['Done','Pending'].includes(b.textContent)) return;
    b.classList.toggle('active', b === btn);
  });
  applyFilters();
}
function setAuditFilter(val, btn) {
  _auditFilter = val;
  btn.closest('.filter-row').querySelectorAll('.filter-btn').forEach(b => {
    if (!['All','Flagged','Info Used','Name Only'].includes(b.textContent)) return;
    b.classList.toggle('active', b === btn);
  });
  applyFilters();
}

function applyFilters() {
  const q = (document.getElementById('emp-search').value || '').toLowerCase();
  let list = _allEmployees;
  if (_draftFilter !== 'all') list = list.filter(e => (e.draft_status || 'pending') === _draftFilter);
  if (_auditFilter !== 'all') list = list.filter(e => e.audit_status === _auditFilter);
  if (q) list = list.filter(e =>
    (e.full_name||e.name||'').toLowerCase().includes(q) ||
    (e.company||'').toLowerCase().includes(q) ||
    (e.email||'').toLowerCase().includes(q) ||
    (e.title||'').toLowerCase().includes(q) ||
    (e.audit_notes||'').toLowerCase().includes(q)
  );
  renderEmployees(list);
}

function statusClass(s) {
  return 'status status-' + (s || '').toLowerCase().replace(/\\s+/g, '-');
}
function scoreColor(n) {
  return n >= 85 ? '#22c55e' : n >= 70 ? '#eab308' : '#6b7280';
}
function altEmails(v) {
  if (!v) return '<span style="color:#334155">—</span>';
  return v.split(', ').join('<br>');
}

function parseInfoHook(infoStr) {
  if (!infoStr) return null;
  try {
    const d = JSON.parse(infoStr);
    return {
      hook: d.personalization_hook || '',
      mutual: d.mutual_context || '',
      role: d.current_role_summary || '',
      activity: d.recent_activity || '',
      raw: infoStr,
    };
  } catch(e) {
    return { hook: '', mutual: '', role: '', activity: '', raw: infoStr };
  }
}

function buildInfoCell(e) {
  const info = parseInfoHook(e.info);
  if (!info) return '<span style="color:#334155">—</span>';
  const id = 'info-' + e.email.replace(/[^a-z0-9]/gi,'_');
  const primary = info.hook || info.mutual || info.role || info.activity || '';
  const preview = primary.length > 100 ? primary.substring(0,100) + '…' : primary;
  const hasSubstantive = !!(info.hook || info.mutual || info.role || info.activity);
  const badge = hasSubstantive
    ? '<span class="has-info" style="font-size:10px">✓ Research</span>'
    : '<span style="color:#64748b;font-size:10px">⚑ Generic</span>';
  return `${badge}
    ${preview ? `<div class="hook-preview">${preview.replace(/</g,'&lt;')}</div>` : ''}
    <span style="cursor:pointer;font-size:10px;color:#475569" onclick="
      const el=document.getElementById('${id}');
      el.style.display=el.style.display==='none'?'block':'none';
      this.textContent=el.style.display==='none'?'▶ full':'▼ hide';
    ">▶ full</span>
    <pre id="${id}" style="display:none;white-space:pre-wrap;font-size:9px;color:#94a3b8;background:#0f172a;padding:6px;border-radius:4px;margin-top:4px;max-width:340px;max-height:200px;overflow:auto">${JSON.stringify(JSON.parse(info.raw||'{}'), null, 2).replace(/</g,'&lt;')}</pre>`;
}

function buildAuditCell(e) {
  const s = e.audit_status;
  if (!s) return '<span style="color:#334155;font-size:10px">—</span>';
  const map = {
    'real_info_used':       ['audit-badge audit-real-used',   '✓ Used'],
    'real_info_unused':     ['audit-badge audit-real-unused',  '✗ Flagged'],
    'has_name_no_research': ['audit-badge audit-name-only',    'Name only'],
    'no_contact_info':      ['audit-badge audit-no-info',      'No info'],
    'auditing':             ['audit-badge audit-auditing',     'Checking…'],
  };
  const [cls, label] = map[s] || ['audit-badge audit-no-info', s];
  const notes = e.audit_notes ? `<div style="font-size:9px;color:#64748b;margin-top:3px;max-width:180px">${e.audit_notes.substring(0,120).replace(/</g,'&lt;')}</div>` : '';
  return `<span class="${cls}">${label}</span>${notes}`;
}

async function loadJobs() {
  const res = await fetch('/jobs?_=' + Date.now());
  const jobs = await res.json();
  const counts = {};
  let aiCount = 0;
  for (const j of jobs) {
    counts[j.status || 'Unknown'] = (counts[j.status || 'Unknown'] || 0) + 1;
    if (j.is_ai_agent_role) aiCount++;
  }
  let chips = `<div class="chip"><span class="chip-n">${jobs.length}</span>Total</div>`;
  for (const [k, v] of Object.entries(counts)) {
    chips += `<div class="chip"><span class="chip-n">${v}</span>${k}</div>`;
  }
  chips += `<div class="chip"><span class="chip-n">${aiCount}</span>AI Roles</div>`;
  document.getElementById('chips').innerHTML = chips;
  jobs.sort((a, b) => (b.fit_score || 0) - (a.fit_score || 0));
  let rows = '';
  for (const j of jobs) {
    const score = j.fit_score || 0;
    const aiBadge = j.is_ai_agent_role
      ? '<span style="background:#7c3aed;color:white;padding:2px 5px;border-radius:4px;font-size:10px;margin-left:4px">AI</span>'
      : '';
    const titleCell = `<a href="${j.job_url || ''}" target="_blank">${j.title || ''}</a>${aiBadge}`;
    rows += `<tr>
      <td>${titleCell}</td>
      <td>${j.company || ''}</td>
      <td>${j.location || ''}</td>
      <td style="color:${scoreColor(score)};font-weight:bold">${score}</td>
      <td>${j.priority || ''}</td>
      <td><span class="${statusClass(j.status)}">${j.status || ''}</span></td>
      <td>${j.hiring_manager_email || ''}</td>
      <td style="color:#64748b;font-size:11px">${altEmails(j.alternative_emails)}</td>
      <td>${j.source || ''}</td>
      <td>${j.posted_date || ''}</td>
    </tr>`;
  }
  document.getElementById('tbody').innerHTML = rows;
  document.getElementById('title').textContent = `Job Search Dashboard — ${jobs.length} jobs`;
}

async function loadEmployees() {
  const res = await fetch('/employees?_=' + Date.now());
  _allEmployees = await res.json();
  const withResearch = _allEmployees.filter(e => {
    if (!e.info) return false;
    try { const d = JSON.parse(e.info); return !!(d.personalization_hook||d.current_role_summary||d.mutual_context||d.career_background||d.recent_activity); }
    catch(err) { return false; }
  }).length;
  const withLinkedIn = _allEmployees.filter(e => e.linkedin_url).length;
  const withDraft    = _allEmployees.filter(e => e.draft_status === 'done').length;
  const flagged      = _allEmployees.filter(e => e.audit_status === 'real_info_unused').length;
  document.getElementById('emp-chips').innerHTML = `
    <div class="chip"><span class="chip-n">${_allEmployees.length}</span>Total Contacts</div>
    <div class="chip"><span class="chip-n">${withLinkedIn}</span>LinkedIn URLs</div>
    <div class="chip"><span class="chip-n">${withResearch}</span>With Research</div>
    <div class="chip"><span class="chip-n">${withDraft}</span>Drafts Ready</div>
    ${flagged ? `<div class="chip" style="border:1px solid #7f1d1d"><span class="chip-n" style="color:#f87171">${flagged}</span>Flagged</div>` : ''}
  `;
  applyFilters();
}

function renderEmployees(list) {
  let rows = '';
  for (const e of list) {
    const displayName = e.full_name || e.name || '';
    const liCell = e.linkedin_url
      ? `<a href="${e.linkedin_url}" target="_blank">View</a>`
      : '<span style="color:#334155">—</span>';
    const added = e.created_at ? e.created_at.split('T')[0] : '';
    const draftStatus = e.draft_status || 'pending';
    const draftColor = draftStatus === 'done' ? '#22c55e' : draftStatus === 'drafting' ? '#eab308' : '#6b7280';
    const draftBadge = `<span style="background:${draftColor};color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold">${draftStatus}</span>`;
    let draftCell = draftBadge;
    if (e.email_draft) {
      const id = 'draft-' + e.email.replace(/[^a-z0-9]/gi,'_');
      draftCell += `<br><span style="cursor:pointer;font-size:10px;color:#94a3b8" onclick="
        const el=document.getElementById('${id}');
        el.style.display=el.style.display==='none'?'block':'none';
        this.textContent=el.style.display==='none'?'▶ show draft':'▼ hide draft';
      ">▶ show draft</span>
      <pre id="${id}" style="display:none;white-space:pre-wrap;font-size:10px;color:#cbd5e1;background:#1e293b;padding:8px;border-radius:4px;margin-top:4px;max-width:420px;max-height:300px;overflow:auto">${e.email_draft.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</pre>`;
    }
    rows += `<tr>
      <td>${displayName || '<span style="color:#334155">—</span>'}</td>
      <td style="font-size:11px;color:#94a3b8">${e.title || ''}</td>
      <td>${e.company || ''}</td>
      <td style="font-size:11px">${e.email}</td>
      <td>${liCell}</td>
      <td>${buildInfoCell(e)}</td>
      <td>${buildAuditCell(e)}</td>
      <td>${draftCell}</td>
      <td style="color:#475569;font-size:11px">${added}</td>
    </tr>`;
  }
  document.getElementById('emp-tbody').innerHTML = rows || '<tr><td colspan="9" style="color:#475569;text-align:center;padding:24px">No employees match current filters.</td></tr>';
}

function filterEmployees() {
  applyFilters();
}

async function loadAll() {
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('spinning');
  btn.textContent = '…';
  try {
    await Promise.all([loadJobs(), loadEmployees()]);
    document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('last-updated').textContent = 'error: ' + e.message;
  } finally {
    btn.classList.remove('spinning');
    btn.innerHTML = '&#x21bb; Refresh';
  }
}
loadAll();
setInterval(loadAll, 15000);
</script>
</body>
</html>"""
    return html


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()

    # Deduplicate webhook retries from Telegram
    update_id = body.get("update_id")
    if update_id:
        if update_id in _processed_updates:
            return JSONResponse({"ok": True})
        _processed_updates.add(update_id)
        if len(_processed_updates) > 1000:
            oldest = sorted(_processed_updates)[:500]
            _processed_updates.difference_update(oldest)

    message = body.get("message")
    if not message:
        return JSONResponse({"ok": True})

    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id", 0)
    text = message.get("text", "")
    voice = message.get("voice")
    audio = message.get("audio")
    photo = message.get("photo")
    document = message.get("document")

    logger.info("Incoming | user=%d chat=%d text=%s voice=%s", user_id, chat_id, text[:80], bool(voice or audio))

    # Auth check
    if user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user %d", user_id)
        await send_message(chat_id, "Unauthorized.")
        return JSONResponse({"ok": True})

    # Normalize command to lowercase (preserve args) so all commands are case-insensitive
    if text.startswith("/"):
        _space = text.find(" ")
        text = (text[:_space].lower() + text[_space:]) if _space != -1 else text.lower()

    # Bot commands -- handled directly (fast, no background needed)
    # /call and /endcall are handled before queue-based commands
    if text.startswith("/call") and not text.startswith("/chrome"):
        await _handle_command(chat_id, text)
        return JSONResponse({"ok": True})

    if text == "/endcall":
        await _handle_command(chat_id, text)
        return JSONResponse({"ok": True})

    # Gate text messages during an active voice call
    from call_handler import get_manager
    call_mgr = get_manager()
    if call_mgr and call_mgr.is_active and not text.startswith("/"):
        await send_message(
            chat_id,
            "\U0001f3a4 Voice call is active \u2014 speak in the group voice chat!\n"
            "Use /endcall to leave the call first.",
        )
        return JSONResponse({"ok": True})

    # /research runs independently (fetches public data + Ollama analysis)
    if text.startswith("/research"):
        company = text[len("/research"):].strip()
        if not company:
            await send_message(chat_id, "Usage: /research <company name>\nExample: /research Apple Inc")
            return JSONResponse({"ok": True})
        health.record_message()
        asyncio.create_task(_process_research(chat_id, company))
        return JSONResponse({"ok": True})

    # /objective — find companies pursuing a specific goal + what they're each doing
    if text.startswith("/objective"):
        objective = text[len("/objective"):].strip()
        if not objective:
            await send_message(
                chat_id,
                "Usage: /objective <goal or theme>\nExample: /objective improve voice-based AI",
            )
            return JSONResponse({"ok": True})
        health.record_message()
        asyncio.create_task(_process_objective(chat_id, objective))
        return JSONResponse({"ok": True})

    # /imagine is special -- it runs independently (uses Gemini, not Claude)
    if text.startswith("/imagine"):
        prompt = text[len("/imagine"):].strip()
        if not prompt:
            await send_message(chat_id, "Usage: /imagine <description of the image>")
            return JSONResponse({"ok": True})
        health.record_message()
        asyncio.create_task(_process_image_generation(chat_id, prompt))
        return JSONResponse({"ok": True})

    if text.startswith("/"):
        await _handle_command(chat_id, text, user_id=user_id)
        return JSONResponse({"ok": True})

    # Photo message -- download and send to Claude with vision
    if photo:
        # Telegram sends multiple sizes; pick the largest (last in array)
        file_id = photo[-1]["file_id"]
        caption = message.get("caption", "")
        health.record_message()

        target_instance = _resolve_target_instance(caption or "photo", user_id)
        asyncio.create_task(_enqueue_message(QueuedMessage(
            chat_id=chat_id,
            msg_type=MessageType.PHOTO,
            text=caption,
            file_id=file_id,
            instance_id=target_instance.id,
            user_id=user_id,
        )))
        return JSONResponse({"ok": True})

    # Document upload -- save to uploads folder inside memory dir
    if document:
        file_id = document["file_id"]
        file_name = document.get("file_name", f"file_{file_id[:8]}")
        save_dir = os.path.join(MEMORY_DIR, "uploads")
        dest_path = os.path.join(save_dir, file_name)
        health.record_message()
        asyncio.create_task(_handle_document_upload(chat_id, file_id, dest_path, file_name))
        return JSONResponse({"ok": True})

    # Voice / audio message -- transcribe then process
    if voice or audio:
        file_id = (voice or audio)["file_id"]
        caption = message.get("caption", "")
        health.record_message()
        voice_instance = _resolve_target_instance("", user_id)
        asyncio.create_task(_enqueue_message(QueuedMessage(
            chat_id=chat_id,
            msg_type=MessageType.VOICE,
            text=caption,
            file_id=file_id,
            instance_id=voice_instance.id,
            user_id=user_id,
        )))
        return JSONResponse({"ok": True})

    # Skip empty messages
    if not text.strip() and not photo and not voice and not audio and not document:
        return JSONResponse({"ok": True})

    # One-shot direct message: @<id or name> <message>
    # Routes to a specific instance WITHOUT changing the active instance.
    # Supports: @2 hey, @Research what's the status?, @ChatGPT summarize this
    import re as _re
    _oneshot_match = _re.match(r'^@(\S+)\s+([\s\S]+)$', text.strip())
    if _oneshot_match:
        target_ref = _oneshot_match.group(1)
        oneshot_text = _oneshot_match.group(2).strip()
        owner_id = 0 if user_id == ALLOWED_USER_ID else user_id

        # Resolve target: try display number first, then title
        target_inst = None
        if target_ref.isdigit():
            target_inst = instances.get_by_display_num(int(target_ref), owner_id)
        if target_inst is None:
            # Partial title match (case-insensitive)
            for inst in instances.list_all(for_owner_id=owner_id):
                if target_ref.lower() in inst.title.lower():
                    target_inst = inst
                    break

        if target_inst is None:
            # Auto-create a new instance with the given name (or a default title if numeric)
            new_title = target_ref if not target_ref.isdigit() else f"Instance {target_ref}"
            target_inst = instances.create(new_title, owner_id=owner_id, switch_active=False)
            _ensure_worker(target_inst)
            disp_new = instances.display_num(target_inst.id, owner_id)
            await send_message(chat_id, f"✨ Created new instance <b>#{disp_new}: {target_inst.title}</b> (your active instance unchanged)", parse_mode="HTML")

        health.record_message()
        disp = instances.display_num(target_inst.id, owner_id)
        await send_message(chat_id, f"📨 Sending to <b>#{disp}: {target_inst.title}</b> (your active instance unchanged)", parse_mode="HTML")

        async def _oneshot_enqueue():
            try:
                await _enqueue_message(QueuedMessage(
                    chat_id=chat_id,
                    msg_type=MessageType.TEXT,
                    text=oneshot_text,
                    voice_reply=_voice_reply_mode,
                    instance_id=target_inst.id,
                    user_id=user_id,
                ))
            except Exception as e:
                logger.error("One-shot enqueue failed: %s", e)
                await send_message(chat_id, f"Error sending to @{target_ref}: {e}")

        asyncio.create_task(_oneshot_enqueue())
        return JSONResponse({"ok": True})

    # Regular text message -- route to instance and process
    health.record_message()

    async def _route_and_enqueue():
        try:
            target_instance = await _resolve_target_instance_async(text, user_id)
            await _enqueue_message(QueuedMessage(
                chat_id=chat_id,
                msg_type=MessageType.TEXT,
                text=text,
                voice_reply=_voice_reply_mode,
                instance_id=target_instance.id,
                user_id=user_id,
            ))
        except Exception as e:
            logger.error("Failed to route/enqueue message: %s", e)
            await send_message(chat_id, f"Error queuing message: {e}")

    asyncio.create_task(_route_and_enqueue())
    return JSONResponse({"ok": True})


def _resolve_target_instance(text: str, user_id: int = 0):
    """Synchronous instance resolution (for photos etc)."""
    resolve_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    return instances.get_active_for(resolve_owner_id)


async def _resolve_target_instance_async(text: str, user_id: int = 0):
    """Route: secondary users go to their active instance; primary user uses Ollama router."""
    resolve_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    if resolve_owner_id != 0:
        return instances.get_active_for(resolve_owner_id)
    # Primary user: use router to pick among their own instances
    target_instance = instances.get_active_for(0)
    user_insts = instances.list_all(for_owner_id=0)
    if len(user_insts) >= 2:
        try:
            inst_list = [{"id": i.id, "title": i.title} for i in user_insts]
            routed_id = await router.route_message(text, inst_list)
            if routed_id is not None:
                routed = instances.get(routed_id)
                if routed:
                    target_instance = routed
        except Exception as e:
            logger.warning("Router failed, using active instance: %s", e)
    return target_instance


# -- Processing functions ----------------------------------------------------


def _label(instance, response: str, owner_id: int = 0) -> str:
    """Prefix response with instance label when the user has multiple instances."""
    owner_insts = instances.list_all(for_owner_id=owner_id)
    if len(owner_insts) >= 2 and instance:
        disp = instances.display_num(instance.id, owner_id)
        return f"**[#{disp}: {instance.title}]**\n{response}"
    return response


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _context_footer(inst) -> str:
    """Build a context window usage footer for the response."""
    if not inst or not inst.context_window:
        return ""
    used = (inst.last_input_tokens + inst.last_cache_read_tokens
            + inst.last_cache_creation_tokens + inst.last_output_tokens)
    if not used:
        return ""
    pct = (used / inst.context_window) * 100
    cost_str = f" \u00b7 ${inst.session_cost:.3f}" if inst.session_cost else ""
    return f"\n\n\u2014\n\U0001f4ca {_fmt_tokens(used)} / {_fmt_tokens(inst.context_window)} ({pct:.1f}%){cost_str}"


# ── Auto-detect media files in responses ──────────────────────────
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff'}
_VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm', '.avi'}
_MEDIA_PATH_RE = re.compile(
    r'((?:[A-Za-z]:[/\\]|[/\\]{2}|/|~/)[^\s"\'`\)\]>]+\.(?:png|jpg|jpeg|gif|webp|bmp|tiff|mp4|mov|mkv|webm|avi))',
    re.IGNORECASE,
)


async def _extract_and_send_media(chat_id: int, text: str) -> list[str]:
    """Find image/video file paths in response text and send them via Telegram."""
    sent = []
    seen = set()
    for raw_path in _MEDIA_PATH_RE.findall(text):
        path = os.path.expanduser(raw_path)
        if path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path) or os.path.getsize(path) < 1024:
            continue
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in _VIDEO_EXTS:
                success = await send_video(chat_id, path)
            else:
                success = await send_photo(chat_id, path)
            if success:
                sent.append(path)
                logger.info("Auto-sent media from response: %s", path)
        except Exception as e:
            logger.error("Failed to auto-send media %s: %s", path, e)
    return sent


async def _process_message(chat_id: int, text: str, voice_reply: bool = False, instance=None, user_id: int = 0) -> None:
    inst = instance or instances.active
    proc_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id), format_markdown=True)

    start = time.time()

    # Agent-aware memory: agents only get their own domain memory, not personal files or general ChromaDB
    if inst.agent_id:
        from agent_memory import get_agent_context
        memory_context = await asyncio.get_event_loop().run_in_executor(
            None, get_agent_context, inst.agent_id, text
        )
    else:
        memory_context = await memory_handler.search_memory(text, user_id=user_id)

    async def on_progress(progress_text: str):
        await send_message(chat_id, _label(inst, progress_text, proc_owner_id), format_markdown=True)

    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prefixed_text = f"[{sender_name}]: {text}" if sender_name else text
    response = await runner.run(prefixed_text, on_progress=on_progress, memory_context=memory_context, instance=inst)
    elapsed = time.time() - start

    if not response or not response.strip():
        response = "(no text response from Claude — check tool output)"

    logger.info("Claude #%d responded in %.1fs (%d chars)", inst.id, elapsed, len(response))

    # Store in agent memory if this is a specialist agent, then run background self-critique
    if inst.agent_id:
        from agent_memory import store_agent_work
        asyncio.ensure_future(
            asyncio.get_event_loop().run_in_executor(None, store_agent_work, inst.agent_id, text, response)
        )
        asyncio.ensure_future(
            agent_manager._run_post_task_critique(
                inst.agent_id, text, response, chat_id, send_message, instances=instances
            )
        )

    # Store memory before appending footer
    asyncio.ensure_future(memory_handler.store_conversation(text, response, user_id=user_id))
    asyncio.ensure_future(memory_handler.extract_and_save(text, response, user_id=user_id))

    response += _context_footer(inst)
    labeled = _label(inst, response, proc_owner_id)

    if voice_reply:
        await _send_with_voice(chat_id, labeled)
    else:
        await send_message(chat_id, labeled, format_markdown=True)

    # Auto-detect and send any media files referenced in the response
    await _extract_and_send_media(chat_id, response)

    # Log to daily task report
    daily_report.log_task("Claude", text, response)


async def _handle_document_upload(chat_id: int, file_id: str, dest_path: str, file_name: str) -> None:
    """Download a document from Telegram and save it to the user's folder."""
    try:
        await send_message(chat_id, f"📥 Downloading {file_name}...")
        await download_document(file_id, dest_path)
        await send_message(chat_id, f"✅ Saved to: {dest_path}")
    except Exception as e:
        logger.error("Document download failed: %s", e)
        await send_message(chat_id, f"❌ Failed to save {file_name}: {e}")


async def _process_photo_message(chat_id: int, file_id: str, caption: str = "", instance=None, user_id: int = 0) -> None:
    """Handle an incoming photo: download, send to Claude for vision analysis."""
    inst = instance or instances.active
    proc_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    await send_message(chat_id, _label(inst, "\U0001f4f7 Downloading image...", proc_owner_id), format_markdown=True)

    image_path = None
    try:
        image_path = await download_photo(file_id)
    except Exception as e:
        logger.error("Photo download failed: %s", e)
        await send_message(chat_id, _label(inst, f"\u274c Failed to download photo: {e}", proc_owner_id), format_markdown=True)
        return

    await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id), format_markdown=True)

    start = time.time()

    async def on_progress(progress_text: str):
        await send_message(chat_id, _label(inst, progress_text, proc_owner_id), format_markdown=True)

    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prefixed_caption = f"[{sender_name}]: {caption}" if sender_name else caption
    response = await runner.run(prefixed_caption, on_progress=on_progress, image_path=image_path, instance=inst)
    elapsed = time.time() - start

    logger.info("Claude #%d responded to photo in %.1fs (%d chars)", inst.id, elapsed, len(response))
    response += _context_footer(inst)
    await send_message(chat_id, _label(inst, response, proc_owner_id), format_markdown=True)

    # Clean up temp image
    if image_path:
        try:
            os.remove(image_path)
        except OSError:
            pass


async def _process_voice_message(chat_id: int, file_id: str, caption: str = "", instance=None, user_id: int = 0) -> None:
    """Handle an incoming voice/audio message: download, transcribe, process, reply with voice."""
    inst = instance or instances.active
    proc_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    # Step 1: Download and transcribe
    await send_chat_action(chat_id, "typing")
    await send_message(chat_id, _label(inst, "\U0001f3a4 Transcribing voice...", proc_owner_id), format_markdown=True)

    voice_path = None
    try:
        voice_path = await download_voice(file_id)
        transcribed = await transcribe_audio(voice_path)
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await send_message(chat_id, _label(inst, f"\u274c Failed to transcribe voice: {e}", proc_owner_id), format_markdown=True)
        return
    finally:
        if voice_path:
            cleanup_file(voice_path)

    if not transcribed.strip():
        await send_message(chat_id, _label(inst, "\U0001f937 Couldn't understand the voice message.", proc_owner_id), format_markdown=True)
        return

    # Combine caption with transcribed text if present
    raw_prompt = f"{caption}\n\n{transcribed}" if caption else transcribed
    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prompt = f"[{sender_name}]: {raw_prompt}" if sender_name else raw_prompt

    # Show what was transcribed
    await send_message(chat_id, _label(inst, f"\U0001f4dd \"{transcribed}\"", proc_owner_id), format_markdown=True)
    await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id), format_markdown=True)

    start = time.time()

    memory_context = await memory_handler.search_memory(raw_prompt, user_id=user_id)

    async def on_progress(progress_text: str):
        await send_message(chat_id, _label(inst, progress_text, proc_owner_id), format_markdown=True)

    response = await runner.run(prompt, on_progress=on_progress, memory_context=memory_context, instance=inst)
    elapsed = time.time() - start

    if not response or not response.strip():
        response = "(no text response from Claude — check tool output)"

    logger.info("Claude #%d responded in %.1fs (%d chars)", inst.id, elapsed, len(response))

    # Store memory before appending footer
    asyncio.ensure_future(memory_handler.store_conversation(raw_prompt, response, user_id=user_id))
    asyncio.ensure_future(memory_handler.extract_and_save(raw_prompt, response, user_id=user_id))

    # Voice in -> voice + text out
    response += _context_footer(inst)
    await _send_with_voice(chat_id, _label(inst, response, proc_owner_id))

    # Auto-detect and send any media files referenced in the response
    await _extract_and_send_media(chat_id, response)


async def _process_research(chat_id: int, company: str) -> None:
    """Run company intelligence research and send the report."""
    await send_message(
        chat_id,
        f"🔍 Researching <b>{company}</b>...\n"
        "Pulling SEC filings, contracts, and news. This takes ~60s.",
        parse_mode="HTML",
    )
    try:
        report = await research_handler.research_company(company)
        await send_message(chat_id, report, parse_mode="HTML")
    except Exception as e:
        logger.error("Research failed for %s: %s", company, e)
        await send_message(chat_id, f"❌ Research failed: {e}")


async def _process_objective(chat_id: int, objective: str) -> None:
    """Find companies working toward an objective and what each is doing."""
    await send_message(
        chat_id,
        f"🎯 Researching companies pursuing: <b>{objective}</b>\n"
        "Scanning news + running analysis. ~60s.",
        parse_mode="HTML",
    )
    try:
        report = await research_handler.research_objective(objective)
        await send_message(chat_id, report, parse_mode="HTML")
    except Exception as e:
        logger.error("Objective research failed for %s: %s", objective, e)
        await send_message(chat_id, f"❌ Objective research failed: {e}")


async def _process_image_generation(chat_id: int, prompt: str) -> None:
    """Generate an image using Gemini and send it to the user."""
    await send_message(chat_id, "\U0001f3a8 Generating image...")

    image_path = None
    try:
        image_path, description = await generate_image(prompt)
        caption = description[:1024] if description else None
        sent = await send_photo(chat_id, image_path, caption=caption)
        if not sent:
            await send_message(chat_id, "\u274c Failed to send the generated image.")
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        await send_message(chat_id, f"\u274c Image generation failed: {e}")
    finally:
        if image_path:
            try:
                os.remove(image_path)
            except OSError:
                pass


async def _send_with_voice(chat_id: int, response: str) -> None:
    """Send a response as both voice and text. Falls back to text-only if TTS fails or text is too long."""
    # Always send text version
    await send_message(chat_id, response, format_markdown=True)

    # Generate and send voice if response isn't too long
    if len(response) > VOICE_MAX_LENGTH:
        logger.info("Response too long for TTS (%d chars > %d), text only", len(response), VOICE_MAX_LENGTH)
        return

    ogg_path = None
    try:
        await send_chat_action(chat_id, "record_voice")
        ogg_path = await text_to_speech(response)
        await send_voice(chat_id, ogg_path)
    except Exception as e:
        logger.error("TTS failed, text-only fallback: %s", e)
    finally:
        if ogg_path:
            cleanup_file(ogg_path)


async def _delayed_restart() -> None:
    """Wait briefly so the webhook response reaches Telegram, then restart."""
    await asyncio.sleep(1)
    await runner.kill_all()
    await close_client()
    logger.info("Server restart requested via /server")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", HOST, "--port", str(PORT)],
    )


async def _handle_command(chat_id: int, text: str, user_id: int = 0) -> None:
    cmd = text.split()[0].lower()
    # owner_id=0 means primary user pool; non-zero means that user's own pool
    owner_id = 0 if user_id == ALLOWED_USER_ID else user_id

    if cmd == "/start":
        await send_message(
            chat_id,
            "Welcome to the Telegram-Claude Bridge!\n\n"
            "Send me any message and I'll forward it to Claude Code "
            "running on your local machine. Claude remembers your "
            "conversation until you start a new one.\n\n"
            "Messages sent while Claude is busy are queued (up to 10) "
            "and processed in order.\n\n"
            "You can also send voice notes! I'll transcribe them "
            "and reply with both text and voice.\n\n"
            "Commands:\n"
            "/imagine &lt;prompt&gt; \u2014 Generate an image\n"
            "/research &lt;company&gt; \u2014 Company intel: vendors, contracts, forecast\n"
            "/objective &lt;goal&gt; \u2014 Companies pursuing an objective + what each is doing\n"
            "/call \u2014 Join group voice chat for live conversation\n"
            "/endcall \u2014 Leave voice chat\n"
            "/stop \u2014 Stop current task & clear queue\n"
            "/kill \u2014 Force-kill all Claude processes\n"
            "/new \u2014 Start a new conversation\n"
            "/voice \u2014 Toggle voice replies for text messages\n"
            "/chrome \u2014 Toggle Chrome browser integration\n"
            "/remember &lt;text&gt; \u2014 Save to memory\n"
            "/task \u2014 View/manage task list (add, done)\n"
            "/memory \u2014 Memory stats &amp; re-index\n"
            "/server \u2014 Restart the bridge server\n"
            "**\U0001f4bb System**\n"
            "/status \u2014 Server status\n"
            "/help \u2014 Show this help",
        )

    elif cmd == "/call":
        from call_handler import start_call

        async def call_status(text):
            await send_message(chat_id, text)

        await start_call(on_status=call_status)

    elif cmd == "/endcall":
        from call_handler import end_call, get_manager
        mgr = get_manager()
        if mgr and mgr.is_active:
            await end_call()
        else:
            await send_message(chat_id, "No active call.")

    elif cmd == "/getid":
        await send_message(chat_id, f"Chat ID: <code>{chat_id}</code>", parse_mode="HTML")

    elif cmd == "/stop":
        inst = instances.get_active_for(owner_id)
        # Clear this instance's queue
        cleared = inst.clear_queue()
        # Stop this instance's Claude process (the worker will see was_stopped
        # and gracefully move to the next queued item)
        stopped = await runner.stop(inst)
        # Only cancel task if there's no process to kill (e.g. stuck on send_message)
        task_cancelled = False
        if not stopped and inst.current_task and not inst.current_task.done():
            inst.current_task.cancel()
            task_cancelled = True

        label = f" [#{instances.display_num(inst.id, owner_id)}: {inst.title}]" if len(instances.list_all(for_owner_id=owner_id)) >= 2 else ""
        parts = []
        if stopped or task_cancelled:
            parts.append("Stopped current task.")
        if cleared:
            parts.append(f"Cleared {cleared} queued message{'s' if cleared != 1 else ''}.")
        if parts:
            await send_message(chat_id, f"\U0001f6d1 " + " ".join(parts) + label)
        else:
            await send_message(chat_id, f"Nothing running and queue is empty.{label}")

    elif cmd == "/kill":
        # Nuclear option: kill everything across all instances
        for inst in instances.list_all():
            inst.clear_queue()
            if inst.current_task and not inst.current_task.done():
                inst.current_task.cancel()
        await runner.stop_all(instances.list_all())
        await runner.kill_all()
        await send_message(chat_id, "\U0001f480 Killed all Claude processes. All queues cleared.")

    elif cmd == "/voice":
        global _voice_reply_mode
        _voice_reply_mode = not _voice_reply_mode
        status = "ON" if _voice_reply_mode else "OFF"
        await send_message(chat_id, f"\U0001f50a Voice replies for text messages: {status}")

    elif cmd == "/chrome":
        if hasattr(runner, 'chrome_enabled'):
            runner.chrome_enabled = not runner.chrome_enabled
        status = "ON" if getattr(runner, 'chrome_enabled', False) else "OFF"
        await send_message(chat_id, f"\U0001f310 Chrome browser integration: {status}")

    elif cmd == "/new":
        inst = instances.get_active_for(owner_id)
        inst.clear_queue()
        await runner.stop(inst)
        if inst.current_task and not inst.current_task.done():
            inst.current_task.cancel()
        runner.new_session(inst)
        label = f" [#{instances.display_num(inst.id, owner_id)}: {inst.title}]" if len(instances.list_all(for_owner_id=owner_id)) >= 2 else ""
        await send_message(chat_id, f"\U0001f195 New conversation started. Queue cleared.{label}")

    elif cmd == "/server":
        await send_message(chat_id, "\U0001f504 Restarting server...")
        # Delay restart so the webhook can return 200 to Telegram first,
        # otherwise Telegram retries the update and causes a restart loop.
        asyncio.create_task(_delayed_restart())

    elif cmd == "/status":
        info = health.get_status()
        uptime_min = info["uptime_seconds"] / 60
        claude_ok = "\u2705" if info["claude_available"] else "\u274c"

        # Per-instance status (scoped to requesting user's pool)
        _active_for_user = instances.get_active_for(owner_id)
        inst_lines = []
        for disp_num, inst in enumerate(instances.list_all(for_owner_id=owner_id), start=1):
            marker = "\u25b6" if inst.id == _active_for_user.id else " "
            status = "busy" if inst.processing else "idle"
            q = inst.queue.qsize() if inst.queue else 0
            inst_lines.append(f"{marker}#{disp_num} {inst.title}: {status} (queue: {q})")
        inst_status = "\n".join(inst_lines)

        from call_handler import get_manager
        call_mgr = get_manager()
        call_state = call_mgr.state if call_mgr else "idle"

        await send_message(
            chat_id,
            f"Server uptime: {uptime_min:.1f} min\n"
            f"Messages processed: {info['message_count']}\n"
            f"Claude CLI available: {claude_ok}\n\n"
            f"Instances:\n{inst_status}\n\n"
            f"Voice call: {call_state}",
        )

    elif cmd == "/help":
        voice_status = "ON" if _voice_reply_mode else "OFF"
        chrome_status = "ON" if getattr(runner, 'chrome_enabled', False) else "OFF"
        from call_handler import get_manager
        call_mgr = get_manager()
        call_status = call_mgr.state if (call_mgr and call_mgr.is_active) else "off"
        active = instances.get_active_for(owner_id)
        user_inst_count = len(instances.list_all(for_owner_id=owner_id))
        inst_info = f"Active: #{instances.display_num(active.id, owner_id)} ({active.title})" if user_inst_count >= 2 else "1 instance running"
        help_text = (
            "**Available Commands:**\n\n"
            "**\U0001f3a8 Image Generation**\n"
            "/imagine <prompt> \u2014 Generate an image\n\n"
            "**\U0001f50d Research & Intel**\n"
            "/research <company> \u2014 Company intel report: vendors, contracts, SEC filings, tactical forecast\n"
            "/objective <goal> \u2014 Who is pursuing an objective + what each company is doing toward it\n\n"
            "**\U0001f916 Orchestration**\n"
            "/orch <task> \u2014 Break task into parallel agents, synthesize results\n"
            "/pipeline \u2014 Run full job search pipeline (scrape \u2192 research \u2192 draft \u2192 audit)\n\n"
            "**\U0001f916 Agents**\n"
            "/agent list \u2014 Show all agents\n"
            "/agent create <type> <name> \u2014 Create a specialist agent  _→ /agent create research News Hound_\n"
            "/agent talk <name> \u2014 Talk directly to an agent  _→ /agent talk News Hound_\n"
            "/agent back \u2014 Return to default instance\n"
            "/agent task <name> <task> \u2014 Assign a one-off task  _→ /agent task News Hound summarize AI news_\n"
            "/agent fix <name> <rule> \u2014 Patch a rule into agent's prompt  _→ /agent fix News Hound always cite sources_\n"
            "/agent feedback <name> <issue> \u2014 Record feedback & auto-improve  _→ /agent feedback News Hound missed the SEC angle_\n"
            "/agent delete <name> \u2014 Delete an agent\n"
            "_Types: research, analytics, writing, job\\_search, coding, manager_\n\n"
            "**\U0001f4dc Instances (Multi-Chat)**\n"
            f"_{inst_info}_\n"
            "Each instance is a separate Claude Code session with its own conversation history. "
            "They don't share context \u2014 you can have one researching while another codes.\n"
            "Instances run concurrently \u2014 you can send messages to different instances without waiting.\n"
            "/claude new <title> \u2014 Spin up a new independent Claude session\n"
            "/claude list \u2014 Show all running instances with IDs & titles\n"
            "/claude switch <id/title> [new_title] \u2014 Switch instance (creates if missing, renames if new_title given)\n"
            "/claude rename <id> <title> \u2014 Rename an instance\n"
            "/claude end <id> \u2014 Close an instance (can't close the last one)\n"
            "/claude \u2014 Show active instance & subcommands\n"
            "_When 2+ instances exist, responses are labeled. Mention an instance by name or # to auto-route._\n"
            "@<id or name> <message> \u2014 One-shot to a specific instance without switching active (creates if missing)\n\n"
            "**\U0001f3a4 Voice**\n"
            f"/call \u2014 Join group voice chat [{call_status}]\n"
            "/endcall \u2014 Leave voice chat\n"
            f"/voice \u2014 Toggle voice replies [{voice_status}]\n\n"
            "**\u2699\ufe0f Control**\n"
            "/new \u2014 Reset conversation for the active instance\n"
            "/stop \u2014 Stop current task & clear queue (active instance only)\n"
            "/kill \u2014 Force-kill all Claude processes across all instances\n"
            f"/chrome \u2014 Toggle Chrome browser [{chrome_status}]\n"
            f"/model sonnet|opus \u2014 Switch model for active instance [{active.model.split("-")[1].capitalize()}]\n\n"
            "**\U0001f9e0 Memory & Tasks**\n"
            "/remember <text> \u2014 Save to memory\n"
            "/task \u2014 View/manage task list\n"
            "/memory \u2014 Memory stats & re-index\n\n"

            "**\U0001f4bb System**\n"
            "/status \u2014 Server status & queue depth\n"
            "/server \u2014 Restart bridge server\n"
            "/help \u2014 Show this help\n\n"
            "Messages are queued per instance (up to 10 each). "
            "Different instances process concurrently."
        )
        await send_message(chat_id, help_text, format_markdown=True)

    elif cmd == "/task":
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "add" and len(parts) > 2:
            result = task_handler.add_task(parts[2])
            await send_message(chat_id, f"\u2705 {result}")
        elif sub == "done" and len(parts) > 2:
            try:
                num = int(parts[2])
                result = task_handler.done_task(num)
                await send_message(chat_id, result)
            except ValueError:
                await send_message(chat_id, "Usage: /task done <number>")
        else:
            result = task_handler.list_tasks()
            await send_message(chat_id, result)

    elif cmd == "/remember":
        text_to_remember = text[len("/remember"):].strip()
        if not text_to_remember:
            await send_message(chat_id, "Usage: /remember <text to save>")
        else:
            result = await memory_handler.remember(text_to_remember, user_id=user_id)
            await send_message(chat_id, f"\U0001f4be {result}")

    elif cmd == "/memory":
        parts = text.split()
        if len(parts) > 1 and parts[1].lower() == "reindex":
            await send_message(chat_id, "\U0001f504 Re-indexing memory files...")
            count = await memory_handler.reindex(user_id=user_id)
            await send_message(chat_id, f"\u2705 Re-indexed {count} chunks from text files.")
        else:
            stats = await memory_handler.get_stats(user_id=user_id)
            if not stats.get("enabled"):
                await send_message(chat_id, "Memory is disabled.")
            elif "error" in stats:
                await send_message(chat_id, f"Memory error: {stats['error']}")
            else:
                await send_message(
                    chat_id,
                    f"\U0001f9e0 Memory Stats:\n"
                    f"Total entries: {stats['total_entries']}\n"
                    f"Collection: {stats['collection']}\n"
                    f"Text files: {stats['text_files']}\n"
                    f"Memory dir: {stats['memory_dir']}\n"
                    f"Remembered file: {'Yes' if stats['remembered_file'] else 'No'}\n\n"
                    f"Use /memory reindex to re-index text files.",
                )

    elif cmd == "/record":
        if screen_recorder.is_recording():
            await send_message(chat_id, f"Already recording. {screen_recorder.status()}\nUse /stoprecord to stop.")
        else:
            path = screen_recorder.start()
            if path:
                await send_message(chat_id, f"\U0001f534 Screen recording started (max {screen_recorder.MAX_DURATION}s).\nUse /stoprecord to stop and receive the video.")
            else:
                await send_message(chat_id, "\u274c Failed to start screen recording. Is ffmpeg installed?")

    elif cmd == "/stoprecord":
        if not screen_recorder.is_recording():
            await send_message(chat_id, "No recording in progress.")
        else:
            await send_message(chat_id, "\u23f9 Stopping recording...")
            video_path = screen_recorder.stop()
            if video_path:
                size_mb = os.path.getsize(video_path) / (1024 * 1024)
                if size_mb > 50:
                    await send_message(chat_id, f"\u26a0\ufe0f Recording is {size_mb:.1f}MB (Telegram limit is 50MB). File saved at: {video_path}")
                else:
                    sent = await send_video(chat_id, video_path, caption="Screen recording")
                    if sent:
                        try:
                            os.remove(video_path)
                        except OSError:
                            pass
                    else:
                        await send_message(chat_id, f"\u274c Failed to send video. File saved at: {video_path}")
            else:
                await send_message(chat_id, "\u274c Recording file was empty or missing.")

    elif cmd == "/pipeline":
        global _pipeline_running
        if _pipeline_running:
            await send_message(chat_id, "⚠️ Pipeline is already running. Check back later.")
        else:
            _pipeline_running = True
            asyncio.create_task(_run_pipeline(chat_id))
            await send_message(chat_id, "🚀 Pipeline started!\n\nRunning:\n1. Scrape LinkedIn\n2. Find hiring contacts\n3. Draft emails\n4. Audit quality\n\nWorkflow 3 sends at 9AM/3PM Warsaw. I'll update you after each step.")

    elif cmd == "/model":
        parts = text.split()
        if len(parts) < 2:
            inst = instances.get_active_for(owner_id)
            await send_message(chat_id, f"Current model for <b>#{instances.display_num(inst.id, owner_id)}</b>: <code>{inst.model}</code>\n\nUsage: /model [sonnet|opus]", parse_mode="HTML")
        else:
            m = parts[1].lower()
            new_model = None
            if "sonnet" in m:
                new_model = "claude-sonnet-4-6"
            elif "opus" in m:
                new_model = "claude-opus-4-6"

            if new_model:
                inst = instances.get_active_for(owner_id)
                inst.model = new_model
                await send_message(chat_id, f"\u2705 Model for <b>#{instances.display_num(inst.id, owner_id)}</b> set to <code>{new_model}</code>", parse_mode="HTML")
            else:
                await send_message(chat_id, "\u274c Invalid model. Choose 'sonnet' or 'opus'.")

    elif cmd == "/claude":
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "new":
            title = arg or "Untitled"
            inst = instances.create(title, owner_id=owner_id)
            _ensure_worker(inst)
            await send_message(
                chat_id,
                f"\u2728 Created instance <b>#{instances.display_num(inst.id, owner_id)}: {inst.title}</b> (now active)",
                parse_mode="HTML",
            )

        elif sub == "list":
            await send_message(chat_id, instances.format_list(for_owner_id=owner_id), parse_mode="HTML")

        elif sub == "switch":
            if not arg:
                await send_message(chat_id, "Usage: /claude switch <id/title> [new_title]")
            else:
                # Handle potential rename: /claude switch <id/title> <new_title>
                switch_parts = arg.split(maxsplit=1)
                target = switch_parts[0]
                new_title = switch_parts[1] if len(switch_parts) > 1 else None

                inst = instances.switch(target, owner_id=owner_id)
                if inst:
                    if new_title:
                        instances.rename(inst.id, new_title, owner_id=owner_id)
                        await send_message(chat_id, f"\u25b6 Switched to and renamed <b>#{instances.display_num(inst.id, owner_id)}: {new_title}</b>", parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"\u25b6 Switched to <b>#{instances.display_num(inst.id, owner_id)}: {inst.title}</b>", parse_mode="HTML")
                    _ensure_worker(inst)
                else:
                    # Not found — create new with the whole 'arg' as title
                    new_inst = instances.create(arg, owner_id=owner_id)
                    _ensure_worker(new_inst)
                    await send_message(
                        chat_id,
                        f"\u2728 Created and switched to <b>#{instances.display_num(new_inst.id, owner_id)}: {new_inst.title}</b>",
                        parse_mode="HTML",
                    )

        elif sub == "rename":
            rename_parts = arg.split(maxsplit=1)
            if len(rename_parts) < 2 or not rename_parts[0].isdigit():
                await send_message(chat_id, "Usage: /claude rename <id> <new title>")
            else:
                disp_num = int(rename_parts[0])
                new_title = rename_parts[1]
                target_inst = instances.get_by_display_num(disp_num, owner_id)
                if target_inst and instances.rename(target_inst.id, new_title, owner_id=owner_id):
                    await send_message(chat_id, f"\u270f\ufe0f Renamed #{disp_num} to <b>{new_title}</b>", parse_mode="HTML")
                else:
                    await send_message(chat_id, f"No instance #{disp_num}. Try /claude list")

        elif sub == "end":
            if not arg or not arg.isdigit():
                await send_message(chat_id, "Usage: /claude end <id>")
            else:
                disp_num = int(arg)
                inst_to_end = instances.get_by_display_num(disp_num, owner_id)
                if inst_to_end:
                    await runner.stop(inst_to_end)
                    inst_to_end.clear_queue()

                removed = instances.remove(inst_to_end.id if inst_to_end else -1, owner_id=owner_id)
                if removed:
                    new_active = instances.get_active_for(owner_id)
                    await send_message(
                        chat_id,
                        f"\U0001f5d1 Ended <b>#{disp_num}: {removed.title}</b>\n"
                        f"Active: #{instances.display_num(new_active.id, owner_id)}: {new_active.title}",
                        parse_mode="HTML",
                    )
                else:
                    owner_inst_count = len(instances.list_all(for_owner_id=owner_id))
                    if inst_to_end and owner_inst_count <= 1:
                        await send_message(chat_id, "Can't end the last instance.")
                    else:
                        await send_message(chat_id, f"No instance #{disp_num}. Try /claude list")

        else:
            inst = instances.get_active_for(owner_id)
            await send_message(
                chat_id,
                f"Active: <b>#{instances.display_num(inst.id, owner_id)}: {inst.title}</b>\n\n"
                f"Commands:\n"
                f"/claude new &lt;title&gt; \u2014 New instance\n"
                f"/claude list \u2014 Show all instances\n"
                f"/claude switch &lt;id/title&gt; [new_title] \u2014 Switch/Create/Rename\n"
                f"/claude rename &lt;id&gt; &lt;title&gt; \u2014 Rename\n"
                f"/claude end &lt;id&gt; \u2014 End instance",
                parse_mode="HTML",
            )

    elif cmd == "/agent":
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "list":
            await send_message(chat_id, agent_manager.format_agent_list(instances), parse_mode="HTML")

        elif sub == "create":
            # /agent create <type> <name>  OR  /agent create <name> (custom type)
            create_parts = arg.split(maxsplit=1)
            if not create_parts:
                await send_message(chat_id,
                    "Usage: /agent create &lt;type&gt; &lt;name&gt;\n"
                    f"Types: {', '.join(SKILL_PACKS.keys())}\n"
                    "Example: /agent create research My Researcher",
                    parse_mode="HTML")
            else:
                type_or_name = create_parts[0].lower()
                is_proactive_type = type_or_name == "proactive"
                if (type_or_name in SKILL_PACKS or is_proactive_type) and len(create_parts) > 1:
                    agent_type = type_or_name
                    agent_name = create_parts[1]
                else:
                    agent_type = "custom"
                    agent_name = arg
                agent_id = re.sub(r"[^a-z0-9_]", "_", agent_name.lower())[:20]
                try:
                    from agent_skills import DEFAULT_AGENT_PROMPTS
                    system_prompt = DEFAULT_AGENT_PROMPTS.get(agent_type, "")
                    new_agent = create_agent(
                        agent_id=agent_id,
                        name=agent_name,
                        agent_type=agent_type,
                        system_prompt=system_prompt,
                        skills=[agent_type] if agent_type in SKILL_PACKS else [],
                    )
                    if is_proactive_type:
                        await send_message(chat_id,
                            f"🤖 Proactive agent created: <b>{new_agent.name}</b>\n"
                            f"ID: <code>{new_agent.id}</code>\n\n"
                            f"Now set its schedule and task:\n"
                            f"<code>/agent proactive {new_agent.id} set 09:00 your task here</code>\n"
                            f"<code>/agent proactive {new_agent.id} set every 2h your task here</code>\n\n"
                            f"Then start the worker:\n"
                            f"<code>/agent proactive start</code>",
                            parse_mode="HTML")
                    else:
                        await send_message(chat_id,
                            f"Agent created: <b>{new_agent.name}</b>\n"
                            f"ID: {new_agent.id} | Type: {new_agent.agent_type}\n"
                            f"Use /agent talk {new_agent.id} to start talking to it.",
                            parse_mode="HTML")
                except ValueError as e:
                    await send_message(chat_id, f"Error: {e}")

        elif sub in ("talk", "switch"):
            if not arg:
                await send_message(chat_id, "Usage: /agent talk &lt;agent name or id&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(arg)
                if target is None:
                    await send_message(chat_id, f"Agent '{arg}' not found. Try /agent list")
                else:
                    inst = agent_manager.talk_to_agent(target.id, instances, owner_id)
                    if inst:
                        await send_message(chat_id,
                            f"Switched to <b>{target.name}</b>\n"
                            f"You're now talking directly to this agent. "
                            f"Use /agent talk Default or /new to go back.",
                            parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"Failed to spawn {target.name}.")

        elif sub == "back":
            # Switch back to the first non-agent instance (Default)
            default_inst = None
            for inst in instances.list_all(for_owner_id=owner_id):
                if not inst.agent_id:
                    default_inst = inst
                    break
            if default_inst:
                instances.set_active_for(owner_id, default_inst.id)
                await send_message(chat_id, f"Switched back to <b>{default_inst.title}</b>", parse_mode="HTML")
            else:
                await send_message(chat_id, "No default instance found.")

        elif sub == "task":
            # /agent task <name> <task description>
            task_parts = arg.split(maxsplit=1)
            if len(task_parts) < 2:
                await send_message(chat_id, "Usage: /agent task &lt;agent&gt; &lt;task description&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(task_parts[0])
                task_desc = task_parts[1]
                if target is None:
                    await send_message(chat_id, f"Agent '{task_parts[0]}' not found. Try /agent list")
                else:
                    queued = await agent_manager.assign_task(target.id, task_desc, chat_id, instances, send_message, owner_id)
                    if queued:
                        await send_message(chat_id, f"Task queued for <b>{target.name}</b>: {task_desc[:100]}", parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"Failed to queue task for {target.name} (queue full or agent not found).")

        elif sub == "schedule":
            # /agent schedule <name> <HH:MM> <task description>
            sched_parts = arg.split(maxsplit=2)
            if len(sched_parts) < 3:
                await send_message(chat_id,
                    "Usage: /agent schedule &lt;agent&gt; &lt;HH:MM&gt; &lt;task&gt;\n"
                    "Example: /agent schedule research 09:00 daily AI market briefing",
                    parse_mode="HTML")
            else:
                target = resolve_agent(sched_parts[0])
                time_str = sched_parts[1]
                task_desc = sched_parts[2]
                if target is None:
                    await send_message(chat_id, f"Agent '{sched_parts[0]}' not found.")
                else:
                    result = agent_manager.schedule_agent_task(target.id, time_str, task_desc)
                    await send_message(chat_id, result)

        elif sub == "pipeline":
            # /agent pipeline Research → Analytics "task"
            if not arg:
                await send_message(chat_id,
                    "Usage: /agent pipeline &lt;agent1&gt; → &lt;agent2&gt; \"task\"\n"
                    "Example: /agent pipeline research → analytics \"AI funding trends\"",
                    parse_mode="HTML")
            else:
                agent_ids, task_desc = agent_manager.parse_pipeline_command(arg)
                if len(agent_ids) < 2 or not task_desc:
                    await send_message(chat_id,
                        "Need at least 2 agents and a quoted task.\n"
                        "Example: /agent pipeline research → analytics \"AI funding trends\"")
                else:
                    async def _run_pipeline():
                        result = await agent_manager.run_pipeline(
                            agent_ids, task_desc, chat_id, instances, send_message, owner_id
                        )
                        await send_message(chat_id, result, format_markdown=True)
                    asyncio.create_task(_run_pipeline())

        elif sub == "skills":
            if arg:
                target = resolve_agent(arg)
                if target is None:
                    await send_message(chat_id, f"Agent '{arg}' not found.")
                else:
                    from agent_memory import get_agent_graph_summary
                    graph_info = get_agent_graph_summary(target.id)
                    skills_text = "\n".join(f"  {s}" for s in target.skills) if target.skills else "  (none)"
                    await send_message(chat_id,
                        f"<b>{target.name}</b>\n"
                        f"Type: {target.agent_type} | Model: {target.model}\n"
                        f"Skills:\n{skills_text}\n"
                        f"Collaborators: {', '.join(target.collaborators) or 'none'}\n"
                        f"{graph_info}",
                        parse_mode="HTML")
            else:
                await send_message(chat_id, list_skills())

        elif sub == "update":
            # /agent update <name> prompt=<text>  OR  name=<new name>
            update_parts = arg.split(maxsplit=1)
            if len(update_parts) < 2:
                await send_message(chat_id, "Usage: /agent update &lt;agent&gt; prompt=&lt;new prompt&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(update_parts[0])
                field_val = update_parts[1]
                if target is None:
                    await send_message(chat_id, f"Agent '{update_parts[0]}' not found.")
                elif "=" not in field_val:
                    await send_message(chat_id, "Format: field=value (e.g. prompt=You are...)")
                else:
                    field, value = field_val.split("=", 1)
                    field = field.strip()
                    value = value.strip().strip('"')
                    updated = update_agent(target.id, **{field: value})
                    if updated:
                        # Update running instance if active
                        running = agent_manager.get_running_instance(target.id, instances)
                        if running and field == "system_prompt":
                            from agent_skills import build_skills_prompt
                            running.agent_system_prompt = value + "\n\n" + build_skills_prompt(updated.skills)
                        await send_message(chat_id, f"Updated <b>{target.name}</b>: {field} changed.", parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"Update failed.")

        elif sub == "delete":
            if not arg:
                await send_message(chat_id, "Usage: /agent delete &lt;agent name or id&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(arg)
                if target is None:
                    await send_message(chat_id, f"Agent '{arg}' not found.")
                else:
                    # If running, end the instance first
                    running = agent_manager.get_running_instance(target.id, instances)
                    if running:
                        instances.remove(running.id, owner_id=owner_id)
                    deleted = delete_agent(target.id)
                    if deleted:
                        await send_message(chat_id, f"Deleted agent: {target.name}")
                    else:
                        await send_message(chat_id, f"Delete failed.")

        elif sub == "fix":
            # /agent fix <name> "rule to add"
            fix_parts = arg.split(maxsplit=1)
            if len(fix_parts) < 2:
                await send_message(chat_id,
                    "Usage: /agent fix &lt;agent&gt; &lt;rule&gt;\n"
                    "Example: /agent fix research Always cite sources with full URLs",
                    parse_mode="HTML")
            else:
                target = resolve_agent(fix_parts[0])
                rule = fix_parts[1].strip().strip('"')
                if target is None:
                    await send_message(chat_id, f"Agent '{fix_parts[0]}' not found. Try /agent list")
                else:
                    await send_message(chat_id, f"Updating {target.name}'s prompt...", parse_mode="HTML")
                    msg = await agent_manager.fix_agent_prompt(target.id, rule, instances=instances)
                    await send_message(chat_id, msg, parse_mode="HTML")

        elif sub == "feedback":
            # /agent feedback <name> "what was wrong"
            fb_parts = arg.split(maxsplit=1)
            if len(fb_parts) < 2:
                await send_message(chat_id,
                    "Usage: /agent feedback &lt;agent&gt; &lt;what was wrong&gt;\n"
                    "Example: /agent feedback research You forgot to cite sources and gave speculation as fact",
                    parse_mode="HTML")
            else:
                target = resolve_agent(fb_parts[0])
                feedback_text = fb_parts[1].strip().strip('"')
                if target is None:
                    await send_message(chat_id, f"Agent '{fb_parts[0]}' not found. Try /agent list")
                else:
                    await send_message(chat_id, f"Processing feedback for {target.name}...", parse_mode="HTML")
                    msg = await agent_manager.record_agent_feedback(target.id, feedback_text, instances=instances)
                    await send_message(chat_id, msg, parse_mode="HTML")

        elif sub == "proactive":
            # /agent proactive list
            # /agent proactive status
            # /agent proactive <name> on
            # /agent proactive <name> off
            # /agent proactive <name> set <HH:MM> <task>
            # /agent proactive <name> clear
            if not arg or arg.strip() in ("list", "status"):
                running = proactive_worker.is_running()
                worker_status = "🟢 Worker running" if running else "🔴 Worker stopped — use /agent proactive start"
                await send_message(chat_id, f"{worker_status}\n\n{proactive_worker.status()}", parse_mode="HTML")
            elif arg.strip() == "start":
                if proactive_worker.is_running():
                    await send_message(chat_id, "Proactive worker is already running.")
                else:
                    await proactive_worker.start(instances, send_message, chat_id)
                    await send_message(chat_id, "🟢 Proactive worker started. Agents with a schedule will fire automatically.")
            elif arg.strip() == "stop":
                if not proactive_worker.is_running():
                    await send_message(chat_id, "Proactive worker is not running.")
                else:
                    await proactive_worker.stop()
                    await send_message(chat_id, "🔴 Proactive worker stopped. No agents will fire until you restart it.")
            else:
                parts = arg.split(maxsplit=2)
                if len(parts) < 2:
                    await send_message(chat_id,
                        "Usage:\n"
                        "/agent proactive start — start the worker\n"
                        "/agent proactive stop — stop the worker\n"
                        "/agent proactive list — show configured agents\n"
                        "/agent proactive &lt;name&gt; set &lt;HH:MM&gt; &lt;task&gt; — configure\n"
                        "/agent proactive &lt;name&gt; on/off — toggle\n"
                        "/agent proactive &lt;name&gt; clear — wipe config",
                        parse_mode="HTML")
                else:
                    target = resolve_agent(parts[0])
                    if target is None:
                        await send_message(chat_id, f"Agent '{parts[0]}' not found. Try /agent list")
                    else:
                        action = parts[1].lower()
                        if action == "on":
                            msg = agent_manager.configure_proactive(target.id, enabled=True,
                                schedule=target.proactive_schedule, task=target.proactive_task)
                            await send_message(chat_id, msg, parse_mode="HTML")
                        elif action == "off":
                            msg = agent_manager.configure_proactive(target.id, enabled=False)
                            await send_message(chat_id, msg, parse_mode="HTML")
                        elif action == "clear":
                            msg = agent_manager.clear_proactive(target.id)
                            await send_message(chat_id, msg, parse_mode="HTML")
                        elif action == "set":
                            if len(parts) < 3:
                                await send_message(chat_id,
                                    "Usage: /agent proactive &lt;name&gt; set &lt;schedule&gt; &lt;task&gt;\n\n"
                                    "Schedule formats:\n"
                                    "  <code>09:00</code> — daily at 9am NYC\n"
                                    "  <code>every 2h</code> — every 2 hours\n"
                                    "  <code>every 30m</code> — every 30 minutes\n"
                                    "  <code>every 1h30m</code> — every 1.5 hours\n\n"
                                    "Example:\n"
                                    "<code>/agent proactive research set 09:00 summarize top AI news</code>\n"
                                    "<code>/agent proactive research set every 2h check for new job listings</code>",
                                    parse_mode="HTML")
                            else:
                                # Schedule is first token (may be "every 2h" = 2 tokens)
                                remainder = parts[2]
                                # Try "every Xh/Xm" (2-word schedule) first
                                every_match = re.match(r"^(every\s+\S+)\s+(.+)$", remainder, re.IGNORECASE)
                                if every_match:
                                    sched, task_desc = every_match.group(1), every_match.group(2)
                                else:
                                    set_parts = remainder.split(maxsplit=1)
                                    if len(set_parts) < 2:
                                        await send_message(chat_id,
                                            "Need both a schedule and a task description.",
                                            parse_mode="HTML")
                                    else:
                                        sched, task_desc = set_parts[0], set_parts[1]
                                        msg = agent_manager.configure_proactive(
                                            target.id, enabled=True, schedule=sched, task=task_desc)
                                        await send_message(chat_id, msg, parse_mode="HTML")
                                if every_match:
                                    msg = agent_manager.configure_proactive(
                                        target.id, enabled=True, schedule=sched, task=task_desc)
                                    await send_message(chat_id, msg, parse_mode="HTML")
                        else:
                            await send_message(chat_id,
                                f"Unknown action '{action}'. Use: on, off, set, clear",
                                parse_mode="HTML")

        else:
            # Default: show agent help
            active_inst = instances.get_active_for(owner_id)
            agent_label = ""
            if active_inst.agent_id:
                active_agent = get_agent(active_inst.agent_id)
                if active_agent:
                    agent_label = f"\nCurrently talking to: <b>{active_agent.name}</b>"

            await send_message(chat_id,
                f"<b>Agent System</b>{agent_label}\n\n"
                "<b>/agent list</b> — Show all agents\n"
                "  <i>→ /agent list</i>\n\n"
                "<b>/agent create &lt;type&gt; &lt;name&gt;</b> — Create a specialist agent\n"
                "  <i>→ /agent create research News Hound</i>\n\n"
                "<b>/agent talk &lt;name&gt;</b> — Talk directly to an agent\n"
                "  <i>→ /agent talk News Hound</i>\n\n"
                "<b>/agent back</b> — Return to default instance\n"
                "  <i>→ /agent back</i>\n\n"
                "<b>/agent task &lt;name&gt; &lt;task&gt;</b> — Assign a one-off task\n"
                "  <i>→ /agent task News Hound find top AI funding rounds this week</i>\n\n"
                "<b>/agent schedule &lt;name&gt; &lt;HH:MM&gt; &lt;task&gt;</b> — Schedule recurring task\n"
                "  <i>→ /agent schedule News Hound 09:00 daily AI market briefing</i>\n\n"
                "<b>/agent pipeline &lt;a&gt; → &lt;b&gt; \"task\"</b> — Sequential agent pipeline\n"
                "  <i>→ /agent pipeline News Hound → analytics \"AI funding trends\"</i>\n\n"
                "<b>/agent skills [name]</b> — List skill packs or agent's skills\n"
                "  <i>→ /agent skills News Hound</i>\n\n"
                "<b>/agent update &lt;name&gt; field=value</b> — Update agent config\n"
                "  <i>→ /agent update News Hound prompt=Always cite sources with URLs</i>\n\n"
                "<b>/agent fix &lt;name&gt; &lt;rule&gt;</b> — Add/merge a rule into agent's prompt\n"
                "  <i>→ /agent fix News Hound Always output results as numbered lists</i>\n\n"
                "<b>/agent feedback &lt;name&gt; &lt;what was wrong&gt;</b> — Record feedback + auto-improve\n"
                "  <i>→ /agent feedback News Hound You missed the SEC angle and only cited 2 sources</i>\n\n"
                "<b>🤖 Proactive Agents</b>\n"
                "<b>/agent create proactive &lt;name&gt;</b> — Create a proactive agent\n"
                "  <i>→ /agent create proactive Daily Briefing</i>\n"
                "<b>/agent proactive start/stop</b> — Start or stop the worker\n"
                "<b>/agent proactive list</b> — Show all proactive agents + status\n"
                "<b>/agent proactive &lt;name&gt; set &lt;schedule&gt; &lt;task&gt;</b> — Configure\n"
                "  <i>→ /agent proactive research set 09:00 summarize AI news</i>\n"
                "  <i>→ /agent proactive research set every 2h check new jobs</i>\n"
                "  <i>→ /agent proactive research set every 30m monitor prices</i>\n"
                "<b>/agent proactive &lt;name&gt; on/off</b> — Toggle\n"
                "<b>/agent proactive &lt;name&gt; clear</b> — Wipe config\n\n"
                "<b>/agent delete &lt;name&gt;</b> — Delete an agent\n"
                "  <i>→ /agent delete News Hound</i>\n\n"
                f"<b>Types:</b> {', '.join(SKILL_PACKS.keys())}",
                parse_mode="HTML")

    elif cmd == "/orch":
        task = text[len("/orch"):].strip()
        if not task:
            await send_message(
                chat_id,
                "Usage: /orch <complex task description>\n\n"
                "Breaks the task into 2-4 parallel sub-tasks, spins up a Claude agent for each, "
                "runs them concurrently, then synthesizes all results into one response."
            )
        else:
            async def _run_orch():
                result = await task_orchestrator.orchestrate(
                    task, chat_id, instances, send_message
                )
                await send_message(chat_id, result, format_markdown=True)
            asyncio.create_task(_run_orch())

    elif cmd == "/draftemails":
        parts = text.split()
        workers = 3
        if len(parts) > 1:
            try:
                workers = max(1, min(int(parts[1]), 9))
            except ValueError:
                await send_message(chat_id, "Usage: /draftemails [workers 1-9]\n\nExample: /draftemails 5")
                return
        global _batch_running
        if _batch_running:
            await send_message(chat_id, "A batch is already running. Use /draftstatus to check progress.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT COUNT(*) as cnt FROM employee WHERE draft_status = 'pending'")
            row = await cur.fetchone()
            pending = row["cnt"]
        if pending == 0:
            await send_message(chat_id, "All named employees already have drafts. Nothing to do.")
            return
        _batch_running = True
        asyncio.create_task(_run_email_batch(workers, chat_id))
        await send_message(chat_id, f"Batch started — {pending} emails queued, {workers} workers")

    elif cmd == "/draftstatus":
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT draft_status, COUNT(*) as cnt FROM employee GROUP BY draft_status"
            )
            rows = await cur.fetchall()
        counts = {r["draft_status"]: r["cnt"] for r in rows}
        await send_message(
            chat_id,
            f"Email batch status:\n\n"
            f"Running: {'Yes' if _batch_running else 'No'}\n"
            f"Pending: {counts.get('pending', 0)}\n"
            f"In flight: {counts.get('drafting', 0)}\n"
            f"Done: {counts.get('done', 0)}\n"
            f"Skipped: {counts.get('skipped', 0)}",
        )

    else:
        await send_message(chat_id, f"Unknown command: {cmd}\nTry /help")
