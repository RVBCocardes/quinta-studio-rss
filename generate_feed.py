import html
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://quinta-studio.com"

BLOG_URL = "https://quinta-studio.com/blog/"
ALL_URL = "https://quinta-studio.com/blog/?SHOWALL_1=1&SIZEN_1=6"

FEED_FILE = Path("feed.xml")

FEED_TITLE = "Quinta Studio — Blog"
FEED_DESCRIPTION = "Dernières publications du blog de Quinta Studio"
FEED_LINK = BLOG_URL

MAX_ITEMS = 100

REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; QuintaStudioRSS/2.0; "
        "+https://github.com/)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# HTTP
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url):
    """Télécharge une page avec quelques contrôles de sécurité."""

    print(f"GET {url}")

    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type",
        "",
    ).lower()

    if "html" not in content_type:
        raise RuntimeError(
            f"Réponse inattendue pour {url}: "
            f"{content_type}"
        )

    if len(response.content) < 500:
        raise RuntimeError(
            f"Réponse anormalement courte pour {url}"
        )

    return response.text


# ============================================================
# OUTILS
# ============================================================

def clean_text(text):
    text = text or ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def absolute_url(url):
    return urljoin(BASE_URL, url)


def normalize_url(url):
    """
    Normalise une URL pour éviter les doublons.
    """

    parsed = urlparse(url)

    path = parsed.path.rstrip("/") or "/"

    return (
        f"{parsed.scheme}://"
        f"{parsed.netloc}"
        f"{path}"
    )


def parse_date(text):
    """
    Quinta Studio affiche actuellement des dates du type :

        24 July 2026
        15 July 2026
    """

    text = clean_text(text)

    formats = [
        "%d %B %Y",
        "%d %b %Y",
    ]

    for date_format in formats:
        try:
            return datetime.strptime(
                text,
                date_format,
            ).replace(tzinfo=timezone.utc)

        except ValueError:
            continue

    return None


def strip_unwanted_elements(element):
    """
    Supprime les éléments qui n'ont pas leur place dans
    le contenu RSS.
    """

    for tag in element.find_all(
        [
            "script",
            "style",
            "noscript",
            "form",
            "nav",
            "footer",
            "header",
        ]
    ):
        tag.decompose()


# ============================================================
# DETECTION DES ARTICLES
# ============================================================

def is_article_url(url):
    """
    Vérifie qu'une URL appartient au blog et n'est pas
    simplement /blog/.
    """

    normalized = normalize_url(url)

    blog_prefix = normalize_url(BLOG_URL)

    if normalized == blog_prefix:
        return False

    if not normalized.startswith(blog_prefix + "/"):
        return False

    # Les pages de pagination ne sont pas des articles.
    lowered = normalized.lower()

    if "pagen_" in lowered:
        return False

    if "showall_" in lowered:
        return False

    return True


def extract_article_links(soup):
    """
    Extrait les liens d'articles depuis une page de listing.
    """

    articles = {}

    for link in soup.find_all("a", href=True):

        url = absolute_url(link["href"])

        if not is_article_url(url):
            continue

        title = clean_text(
            link.get_text(" ", strip=True)
        )

        if not title:
            continue

        normalized = normalize_url(url)

        # On évite les liens internes parasites.
        if len(title) < 4:
            continue

        articles[normalized] = {
            "url": normalized,
            "title": title,
        }

    return articles


# ============================================================
# LISTE DU BLOG
# ============================================================

def collect_article_links():
    """
    Utilise prioritairement la vue "All".

    Si celle-ci venait à disparaître, on revient sur
    la pagination classique.
    """

    articles = {}

    # --------------------------------------------------------
    # 1. Vue ALL
    # --------------------------------------------------------

    try:
        all_html = fetch(ALL_URL)
        all_soup = BeautifulSoup(
            all_html,
            "html.parser",
        )

        found = extract_article_links(all_soup)

        print(
            f"Vue ALL : {len(found)} articles détectés"
        )

        articles.update(found)

    except Exception as exc:

        print(
            "ATTENTION : impossible de récupérer "
            f"la vue ALL : {exc}"
        )

    # --------------------------------------------------------
    # 2. Page principale
    # --------------------------------------------------------

    try:
        blog_html = fetch(BLOG_URL)

        blog_soup = BeautifulSoup(
            blog_html,
            "html.parser",
        )

        found = extract_article_links(blog_soup)

        articles.update(found)

        # ----------------------------------------------------
        # 3. Pagination détectée automatiquement
        # ----------------------------------------------------

        pagination_urls = []

        for link in blog_soup.find_all(
            "a",
            href=True,
        ):

            text = clean_text(
                link.get_text(" ", strip=True)
            )

            href = absolute_url(link["href"])

            if text.isdigit() and "/blog" in href:
                pagination_urls.append(href)

        pagination_urls = list(
            dict.fromkeys(pagination_urls)
        )

        print(
            "Pages de pagination détectées :",
            len(pagination_urls),
        )

        for page_url in pagination_urls:

            try:

                page_html = fetch(page_url)

                page_soup = BeautifulSoup(
                    page_html,
                    "html.parser",
                )

                found = extract_article_links(
                    page_soup
                )

                articles.update(found)

            except Exception as exc:

                print(
                    f"ATTENTION : page ignorée "
                    f"{page_url}: {exc}"
                )

    except Exception as exc:

        print(
            "ATTENTION : impossible de récupérer "
            f"la page principale : {exc}"
        )

    # --------------------------------------------------------
    # Sécurité
    # --------------------------------------------------------

    if not articles:
        raise RuntimeError(
            "AUCUN ARTICLE DÉTECTÉ. "
            "Le flux existant ne sera pas modifié."
        )

    return list(articles.values())


# ============================================================
# EXTRACTION D'UN ARTICLE
# ============================================================

def extract_title(soup, fallback):
    """
    Le titre H1 est privilégié.
    """

    h1 = soup.find("h1")

    if h1:

        title = clean_text(
            h1.get_text(" ", strip=True)
        )

        if title and title.lower() != "blog":
            return title

    # Secours : balise title
    title_tag = soup.find("title")

    if title_tag:

        title = clean_text(
            title_tag.get_text()
        )

        if title:
            return title

    return fallback


def extract_date(soup):
    """
    Recherche une date dans les premiers éléments
    de la page.
    """

    # <time>
    for time_tag in soup.find_all("time"):

        date = parse_date(
            time_tag.get_text(
                " ",
                strip=True,
            )
        )

        if date:
            return date

    # Recherche générale dans les éléments courts.
    for element in soup.find_all(
        ["div", "span", "p"]
    ):

        text = clean_text(
            element.get_text(
                " ",
                strip=True,
            )
        )

        if len(text) > 40:
            continue

        date = parse_date(text)

        if date:
            return date

    return None


def extract_category(soup):
    """
    Quinta Studio affiche actuellement notamment
    la catégorie "New products".
    """

    # On cherche les liens contenant une catégorie
    # associée au blog.
    for link in soup.find_all(
        "a",
        href=True,
    ):

        text = clean_text(
            link.get_text(
                " ",
                strip=True,
            )
        )

        href = link["href"].lower()

        if not text:
            continue

        if "blog" in href and text.lower() not in {
            "blog",
            "all",
        }:
            return text

    return None


def extract_content(soup):
    """
    Extraction du contenu principal.

    On cherche d'abord un <article>, puis <main>,
    puis une zone contenant le plus de texte.
    """

    candidates = []

    # --------------------------------------------------------
    # ARTICLE
    # --------------------------------------------------------

    for element in soup.find_all("article"):

        clone = BeautifulSoup(
            str(element),
            "html.parser",
        )

        strip_unwanted_elements(clone)

        text = clean_text(
            clone.get_text(
                " ",
                strip=True,
            )
        )

        if len(text) > 100:
            candidates.append(
                (len(text), clone)
            )

    # --------------------------------------------------------
    # MAIN
    # --------------------------------------------------------

    for element in soup.find_all("main"):

        clone = BeautifulSoup(
            str(element),
            "html.parser",
        )

        strip_unwanted_elements(clone)

        text = clean_text(
            clone.get_text(
                " ",
                strip=True,
            )
        )

        if len(text) > 100:
            candidates.append(
                (len(text), clone)
            )

    # --------------------------------------------------------
    # FALLBACK : gros conteneurs
    # --------------------------------------------------------

    if not candidates:

        for element in soup.find_all(
            ["div", "section"]
        ):

            clone = BeautifulSoup(
                str(element),
                "html.parser",
            )

            strip_unwanted_elements(clone)

            text = clean_text(
                clone.get_text(
                    " ",
                    strip=True,
                )
            )

            if 300 < len(text) < 30000:
                candidates.append(
                    (len(text), clone)
                )

    if not candidates:
        return ""

    # Le plus gros bloc de contenu.
    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    content = candidates[0][1]

    # --------------------------------------------------------
    # Nettoyage supplémentaire
    # --------------------------------------------------------

    for tag in content.find_all(
        [
            "script",
            "style",
            "noscript",
            "form",
        ]
    ):
        tag.decompose()

    # Les liens externes sont conservés.
    # Les URLs relatives sont transformées en absolues.
    for tag in content.find_all(
        ["a", "img"],
    ):

        if tag.has_attr("href"):
            tag["href"] = absolute_url(
                tag["href"]
            )

        if tag.has_attr("src"):
            tag["src"] = absolute_url(
                tag["src"]
            )

    return str(content)


def extract_image(soup):
    """
    og:image est privilégié.
    """

    og = soup.find(
        "meta",
        attrs={
            "property": "og:image"
        },
    )

    if og and og.get("content"):
        return absolute_url(
            og["content"]
        )

    # Secours : première image du contenu.
    for image in soup.find_all(
        "img",
        src=True,
    ):

        src = image["src"]

        if src.startswith("data:"):
            continue

        return absolute_url(src)

    return None


def parse_article(article):
    """
    Télécharge et analyse une fiche article.
    """

    url = article["url"]

    try:

        html_content = fetch(url)

        soup = BeautifulSoup(
            html_content,
            "html.parser",
        )

        title = extract_title(
            soup,
            article["title"],
        )

        date = extract_date(soup)

        category = extract_category(
            soup
        )

        content = extract_content(
            soup
        )

        image = extract_image(
            soup
        )

        if not title:
            raise RuntimeError(
                "Titre introuvable"
            )

        if not date:
            raise RuntimeError(
                "Date introuvable"
            )

        if not content:
            raise RuntimeError(
                "Contenu introuvable"
            )

        return {
            "title": title,
            "url": url,
            "date": date,
            "category": category,
            "content": content,
            "image": image,
        }

    except Exception as exc:

        print(
            f"ERREUR article {url}: {exc}"
        )

        return None


# ============================================================
# RSS
# ============================================================

def xml_escape(value):
    return html.escape(
        value or "",
        quote=True,
    )


def cdata(value):
    value = value or ""

    value = value.replace(
        "]]>",
        "]]]]><![CDATA[>",
    )

    return (
        "<![CDATA["
        + value
        + "]]>"
    )


def build_description(article):
    """
    Prépare le contenu affiché par Inoreader.
    """

    content = article["content"]

    image = article.get("image")

    if image:

        image_html = (
            '<p>'
            f'<img src="{xml_escape(image)}" '
            f'alt="{xml_escape(article["title"])}">'
            '</p>'
        )

        content = (
            image_html
            + content
        )

    return content


def build_item(article):

    pub_date = format_datetime(
        article["date"],
        usegmt=True,
    )

    description = build_description(
        article
    )

    category = ""

    if article.get("category"):

        category = (
            f"<category>"
            f"{xml_escape(article['category'])}"
            f"</category>"
        )

    return f"""
    <item>
      <title>{xml_escape(article["title"])}</title>
      <link>{xml_escape(article["url"])}</link>

      <guid isPermaLink="true">
        {xml_escape(article["url"])}
      </guid>

      <pubDate>{pub_date}</pubDate>

      {category}

      <description>
        {cdata(description)}
      </description>

    </item>
"""


def build_feed(articles):

    now = datetime.now(
        timezone.utc
    )

    items = "\n".join(
        build_item(article)
        for article in articles
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>

<rss version="2.0"
     xmlns:atom="http://www.w3.org/2005/Atom">

  <channel>

    <title>{xml_escape(FEED_TITLE)}</title>

    <link>{xml_escape(FEED_LINK)}</link>

    <description>
      {xml_escape(FEED_DESCRIPTION)}
    </description>

    <language>en</language>

    <lastBuildDate>
      {format_datetime(now, usegmt=True)}
    </lastBuildDate>

    <atom:link
      href="https://YOUR-USERNAME.github.io/quinta-studio-rss/feed.xml"
      rel="self"
      type="application/rss+xml"
    />

    {items}

  </channel>

</rss>
"""


# ============================================================
# VALIDATION
# ============================================================

def validate_feed(feed):

    import xml.etree.ElementTree as ET

    try:

        ET.fromstring(feed)

    except ET.ParseError as exc:

        raise RuntimeError(
            f"Flux RSS XML invalide : {exc}"
        )

    if "<item>" not in feed:

        raise RuntimeError(
            "Le flux RSS ne contient aucun item."
        )

    # Contrôle supplémentaire.
    item_count = feed.count(
        "<item>"
    )

    if item_count < 1:

        raise RuntimeError(
            "Le flux RSS semble vide."
        )

    print(
        f"Validation XML OK — "
        f"{item_count} articles"
    )


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():

    print("=" * 60)
    print("QUINTA STUDIO RSS GENERATOR 2.0")
    print("=" * 60)

    # --------------------------------------------------------
    # 1. Liste des articles
    # --------------------------------------------------------

    links = collect_article_links()

    print(
        f"\n{len(links)} fiches détectées."
    )

    # --------------------------------------------------------
    # 2. Analyse des fiches
    # --------------------------------------------------------

    articles = []

    for index, article_link in enumerate(
        links,
        start=1,
    ):

        print(
            f"\n[{index}/{len(links)}] "
            f"{article_link['title']}"
        )

        article = parse_article(
            article_link
        )

        if article:
            articles.append(article)

    # --------------------------------------------------------
    # 3. Contrôle de sécurité
    # --------------------------------------------------------

    if not articles:

        raise RuntimeError(
            "Aucun article n'a pu être analysé."
        )

    # --------------------------------------------------------
    # 4. Tri
    # --------------------------------------------------------

    articles.sort(
        key=lambda article: article["date"],
        reverse=True,
    )

    articles = articles[:MAX_ITEMS]

    print(
        f"\n{len(articles)} articles "
        f"seront placés dans le flux."
    )

    # --------------------------------------------------------
    # 5. Génération
    # --------------------------------------------------------

    feed = build_feed(
        articles
    )

    # --------------------------------------------------------
    # 6. Validation
    # --------------------------------------------------------

    validate_feed(feed)

    # --------------------------------------------------------
    # 7. Écriture
    # --------------------------------------------------------

    FEED_FILE.write_text(
        feed,
        encoding="utf-8",
    )

    print(
        f"\nFlux écrit dans : "
        f"{FEED_FILE}"
    )

    print("=" * 60)
    print("SUCCÈS")
    print("=" * 60)


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            "\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "ERREUR FATALE\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        )

        print(exc)

        # Très important :
        # le workflow GitHub s'arrête ici et ne pousse
        # donc pas un mauvais feed.xml.

        sys.exit(1)
