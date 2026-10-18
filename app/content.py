from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTICLE_DIR = ROOT / "content" / "articles"

PLANS = [
    {
        "slug": "starter",
        "name": "Starter",
        "price": 2700,
        "original": 3600,
        "discount": "23% OFF",
        "blurb": "Perfect for beginners who want to start learning with essential courses and resources.",
        "popular": False,
        "billing": "One-time",
        "access": "No end date is published. Listed course access starts only after payment is confirmed.",
        "support": "Community access is listed. Priority support is not included on this plan.",
        "features": [
            "Access to selected courses",
            "Course progress tracking",
            "Learning resources",
            "Community access",
            "Course updates",
            "Certificate of completion",
        ],
    },
    {
        "slug": "pro",
        "name": "Pro",
        "price": 5500,
        "original": 7333,
        "discount": "25% OFF",
        "blurb": "For serious learners who want complete access and practical learning.",
        "popular": True,
        "billing": "One-time",
        "access": "No end date is published. This plan lists unlimited course access after payment is confirmed.",
        "support": "Priority support and community access are listed on this plan.",
        "features": [
            "Access to all core courses",
            "Unlimited course access",
            "Practical projects and exercises",
            "Course certificates",
            "Community access",
            "New course updates",
            "Priority support",
            "Learning resources",
        ],
    },
    {
        "slug": "premium",
        "name": "Premium",
        "price": 8300,
        "original": 11067,
        "discount": "25% OFF",
        "blurb": "Complete learning experience with advanced courses and personalized support.",
        "popular": False,
        "billing": "One-time",
        "access": "No end date is published. This plan lists access to all courses after payment is confirmed.",
        "support": "Priority support, 1-on-1 guidance, and community access are listed on this plan.",
        "features": [
            "Everything in Pro",
            "Access to all courses",
            "Advanced learning content",
            "Premium resources",
            "1-on-1 guidance",
            "Career and portfolio support",
            "Priority support",
            "Exclusive projects",
            "Early access to new courses",
            "Certificate of completion",
        ],
    },
]

PLAN_RANK = {"starter": 1, "pro": 2, "premium": 3}

TEAM = [
    {
        "slug": "abdul-raof",
        "name": "Abdul Raof",
        "role": "Virtual Assistant | E-commerce, Web Research & Digital Support Specialist",
        "bio": (
            "I'm a reliable Virtual Assistant with experience supporting clients with e-commerce tasks, "
            "web research, data management, and digital business operations. I focus on accurate work, "
            "clear communication, and meeting deadlines. I'm always eager to learn new tools and provide "
            "dependable support that helps clients save time and grow their businesses."
        ),
        "skills": [
            "Virtual Assistance",
            "Web Research",
            "Data Entry",
            "E-commerce Management",
            "Shopify",
            "Product Research",
            "Product Listing",
            "Etsy",
            "Lead Generation",
            "Internet Research",
            "Data Management",
            "Social Media Support",
            "Content Research",
            "SEO Research",
            "Administrative Support",
            "Customer Support",
            "Microsoft Office",
            "Google Workspace",
        ],
        "languages": "English, Urdu.",
        "availability": "Available for freelance and remote projects, with flexible working hours.",
    },
    {
        "slug": "naila-m-ali",
        "name": "Naila M Ali",
        "role": "Graphic Designer & Adobe Stock Contributor",
        "bio": (
            "I'm a Graphic, Branding & Packaging Designer with 3 years of experience in the creative industry. "
            "I create commercial stock assets, AI-assisted visuals, and creative design workflows, with a focus "
            "on helping designers understand and improve their Adobe Stock journey."
        ),
        "skills": [
            "Graphic Design",
            "Adobe Stock",
            "Stock Asset Creation",
            "AI Image Generation",
            "Vector Design",
            "Illustration",
            "Packaging Design",
            "Branding",
            "Photoshop",
            "Illustrator",
            "AI Creative Workflows",
        ],
        "languages": "English, Urdu.",
        "availability": "Available for messages and inquiries from 10:00 AM to 8:00 PM Pakistan time.",
    },
]

FAQS = [
    (
        "What is Digicraft?",
        "Digicraft is an online academy for practical digital skills. You can browse published courses, review one-time plans, and open the chatroom after your email is verified.",
    ),
    (
        "Who are the courses for?",
        "Courses are for people learning practical digital skills such as freelancing, digital platforms, content creation, and e-commerce. A course is listed only after it is published, with its level shown on the card.",
    ),
    (
        "Do I need previous experience?",
        "No previous experience is required to create an account. A published course can still be marked Beginner or another level, and that level is the prerequisite shown for that course.",
    ),
    (
        "Can Digicraft guarantee that I will earn money?",
        "No. Digicraft provides education and guidance. Income and results depend on individual effort, skills, market conditions, and other factors, and are not guaranteed.",
    ),
    (
        "How do I get access to a course?",
        "Create an account with a personal Gmail address, verify your email, and use the plan that course requires. Enrollment is stored on your account. Saving a plan request does not grant access.",
    ),
    (
        "Can I access courses on my phone?",
        "Yes. The site works in a phone, tablet, or desktop browser. Published lessons open in the browser. There is no separate app download.",
    ),
    (
        "How do pricing and billing work?",
        "Each plan has a one-time price in PKR. Choosing a plan saves a request. The plan is marked paid only after a verified Stripe webhook, and only when Stripe is configured. The return page never marks a payment complete.",
    ),
    (
        "Is there a refund or cancellation policy?",
        "No refund or cancellation policy is published. An unpaid plan request does not charge you. Checkout is not offered unless Stripe is configured.",
    ),
    (
        "Are certificates available?",
        "Some plans list a certificate of completion. This website does not generate or download a certificate file.",
    ),
    (
        "What learning support is available?",
        "Enrolled members can post in a lesson discussion. Verified members can use the chatroom. A free consultation request can be sent from the consultation page. Direct WhatsApp, Telegram, email, Zoom, and Google Meet links appear only when those channels are configured. Priority support and 1-on-1 guidance are listed on specific plans; they are not a separate live product on this site.",
    ),
    (
        "Who can use the chatroom?",
        "Only signed-in members with a verified email can read or post. Your email address is not shown. Messages are stored on this server and are not end-to-end encrypted.",
    ),
    (
        "Can I track progress and update my profile?",
        "Yes. After you verify your email and have an active enrollment, completed lessons are saved to your account. Verified members can change their display name and profile photo. Your email address is visible only to you.",
    ),
]

METHOD = [
    (
        "01",
        "Create your account",
        "Register with a personal Gmail address and open the verification link. Member features stay closed until the email is verified.",
    ),
    (
        "02",
        "Choose a course",
        "Open a published course and request the plan it requires. The request stays unpaid until Stripe confirms payment.",
    ),
    (
        "03",
        "Practice and ask",
        "Follow the published lessons. Enrolled members can discuss a lesson, and verified members can use the chatroom.",
    ),
]

CONTACT_METHODS = [
    ("whatsapp", "WhatsApp", "whatsapp_number"),
    ("telegram", "Telegram", "telegram_username"),
    ("email", "Email", "contact_email"),
    ("zoom", "Zoom", "zoom_url"),
    ("google_meet", "Google Meet", "meet_url"),
]


def plan_by_slug(slug: str) -> dict | None:
    return next((plan for plan in PLANS if plan["slug"] == slug), None)


def team_by_slug(slug: str) -> dict | None:
    return next((member for member in TEAM if member["slug"] == slug), None)


def contact_methods(settings) -> list[dict]:
    methods = []
    for key, label, attr in CONTACT_METHODS:
        value = getattr(settings, attr, "")
        href = ""
        if value:
            if key == "whatsapp":
                digits = re.sub(r"[^\d]", "", value)
                href = f"https://wa.me/{digits}" if digits else ""
            elif key == "telegram":
                handle = value[1:] if value.startswith("@") else value
                href = value if value.startswith("http") else f"https://t.me/{handle}"
            elif key == "email":
                href = f"mailto:{value}"
            else:
                href = value if value.startswith("https://") else ""
        methods.append(
            {
                "key": key,
                "label": label,
                "configured": bool(href),
                "href": href,
                "detail": value if href else "",
            }
        )
    return methods


def _inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    def link(match: re.Match) -> str:
        label, href = match.group(1), match.group(2)
        if not href.startswith("/") or href.startswith("//"):
            return label
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, escaped)


def markdown_to_html(source: str) -> str:
    blocks = re.split(r"\n\s*\n", source.strip())
    html_blocks = []
    image_re = re.compile(r"^!\[([^\]]*)\]\(([A-Za-z0-9._-]+)\)$")
    for block in blocks:
        image = image_re.fullmatch(block.strip())
        if image:
            alt, name = image.group(1), image.group(2)
            if (ROOT / "static" / "images" / name).is_file():
                html_blocks.append(
                    '<figure class="article-figure">'
                    f'<img src="/static/images/{html.escape(name, quote=True)}" alt="{html.escape(alt)}" loading="lazy">'
                    "</figure>"
                )
            continue
        lines = block.splitlines()
        if lines and lines[0].startswith("### "):
            html_blocks.append(f"<h3>{_inline(lines[0][4:])}</h3>")
        elif lines and lines[0].startswith("## "):
            html_blocks.append(f"<h2>{_inline(lines[0][3:])}</h2>")
        elif lines and all(line.startswith("- ") for line in lines):
            items = "".join(f"<li>{_inline(line[2:])}</li>" for line in lines)
            html_blocks.append(f"<ul>{items}</ul>")
        elif lines and all(re.match(r"\d+\. ", line) for line in lines):
            items = "".join(f"<li>{_inline(re.sub(r'^\d+\. ', '', line))}</li>" for line in lines)
            html_blocks.append(f"<ol>{items}</ol>")
        else:
            html_blocks.append(f"<p>{_inline(' '.join(lines))}</p>")
    return "\n".join(html_blocks)


def load_articles() -> list[dict]:
    articles = []
    for path in sorted(ARTICLE_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            continue
        _, matter, body = raw.split("---", 2)
        meta = {}
        for line in matter.strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        articles.append(
            {
                "slug": meta["slug"],
                "title": meta["title"],
                "category": meta.get("category", "Guide"),
                "summary": meta.get("summary", ""),
                "published_on": meta.get("published", ""),
                "updated_on": meta.get("updated", ""),
                "author": meta.get("author", "Digicraft"),
                "image": meta.get("image", ""),
                "image_alt": meta.get("image_alt", ""),
                "body_html": markdown_to_html(body),
            }
        )
    articles.sort(key=lambda item: item["published_on"], reverse=True)
    return articles


def format_date(value: str) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        year, month, day = value.split("-")
        return f"{int(day)} {months[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return value
