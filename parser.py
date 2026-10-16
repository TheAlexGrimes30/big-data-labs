from __future__ import annotations

import csv
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGGER = logging.getLogger("rbc_parser")
FEED_URL = "https://www.rbc.ru/short_news"
TIME_PATTERN = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
CATEGORY_NAMES = {
    "Политика", "Экономика", "Общество", "Бизнес", "Технологии и медиа",
    "Финансы", "Спорт", "Недвижимость", "Авто", "РБК Образование",
    "Образование", "Инвестиции", "Биографии", "База знаний",
    "Подписка на РБК", "Life",
}
# Селекторы проверены по сохранённому HTML РБК от 16.10.2026.
FEED_LINK_SELECTORS = (
    "article.info-block-custom-news a.info-block-title",
    "article.info-block a.info-block-title",
    ".item__wrap a.item__link",  # резервный селектор старой разметки
    "a.item__link",
)
TITLE_SELECTORS = ("a.info-block-title", ".item__title", ".item__title-wrap")
CATEGORY_SELECTORS = (".meta-info-row-topic", ".item__category", ".item__rubric")
TIME_SELECTORS = (".meta-info-row-date", ".item__time", ".item__date", "time")
CONTENT_SELECTORS = (
    "[itemprop='articleBody']", ".article__text", ".article__content",
    ".article__body", "article",
)
REMOVE_SELECTORS = (
    "script", "style", "noscript", "nav", "aside", "footer", "form",
    ".article__insert", ".article__banner", ".article__ad",
    ".article__inline-item", ".article__related", ".article__recommend",
    "[class*='advert']", "[class*='banner']", "[class*='recommend']",
    "[class*='related']", "[class*='subscribe']", "[class*='paywall']",
)


@dataclass
class NewsArticle:
    title: str
    url: str
    category: str | None
    published_time: str | None
    published_at: str | None
    scraped_at: str
    source: str = "РБК"
    content: str | None = None
    content_status: str = "not_fetched"


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def canonical_url(value: str, base: str = FEED_URL) -> str:
    parsed = urlsplit(urljoin(base, value))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", ""))


def allowed_article_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return False
    if parsed.hostname != "rbc.ru" and not parsed.hostname.endswith(".rbc.ru"):
        return False
    path = parsed.path
    if path.startswith(("/short_news", "/privacy", "/legal", "/tags", "/story", "/photoreport")):
        return False
    return bool(
        re.search(r"/[0-9a-f]{16,}(?:/|$)", path)
        or re.search(r"/\d{2}/\d{2}/\d{4}/", path)
        or (parsed.hostname == "editorial.rbc.ru" and len(path) > 12)
    )


def first_text(element: Tag, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = element.select_one(selector)
        if node:
            value = normalize_text(node.get_text(" ", strip=True))
            if value:
                return value
    return None


def _metadata(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            value = element.get("content") or element.get("datetime") or element.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _jsonld_articles(soup: BeautifulSoup) -> Iterator[dict[str, Any]]:
    def walk(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, list):
            for entry in value:
                yield from walk(entry)
        elif isinstance(value, dict):
            kinds = value.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if any(kind in ("NewsArticle", "Article", "ReportageNewsArticle") for kind in kinds):
                yield value
            if "@graph" in value:
                yield from walk(value["@graph"])

    for node in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(node.string or node.get_text()))
        except (ValueError, TypeError):
            continue


def _extract_time(soup: BeautifulSoup) -> str | None:
    return _metadata(soup, (
        'meta[property="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[name="datePublished"]',
        'time[datetime]',
    ))


def _extract_paragraphs(container: Tag) -> str | None:
    copy = BeautifulSoup(str(container), "html.parser")
    root = copy.find()
    if root is None:
        return None
    for selector in REMOVE_SELECTORS:
        for node in list(root.select(selector)):
            node.decompose()

    blocks = root.select(".article__text__paragraph, p")
    paragraphs: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        if block.find_parent(class_="article__text__paragraph") is not None:
            continue
        text = normalize_text(block.get_text(" ", strip=True))
        if len(text) < 15 or text in seen:
            continue
        seen.add(text)
        paragraphs.append(text)
    return "\n\n".join(paragraphs) if paragraphs else None


def load_page(driver: webdriver.Chrome, url: str, selector: str, timeout: int) -> None:
    try:
        driver.get(url)
    except TimeoutException:
        LOGGER.warning("Тайм-аут навигации %s — проверяю доступный DOM", url)

    target = urlsplit(url)

    def is_ready(browser: webdriver.Chrome) -> bool:
        current = urlsplit(browser.current_url)
        if current.hostname != target.hostname or current.path.rstrip("/") != target.path.rstrip("/"):
            return False
        # При page_load_strategy='none' ресурсы могут ещё грузиться:
        # достаточно появления нужного элемента, readyState ждать не нужно.
        return bool(browser.find_elements(By.CSS_SELECTOR, selector))

    try:
        WebDriverWait(driver, timeout, poll_frequency=0.5).until(is_ready)
    except TimeoutException as exc:
        try:
            current = driver.current_url
        except WebDriverException:
            current = "(недоступен)"
        raise TimeoutException(
            f"Не дождались элементов {selector!r} на {url}. "
            f"Текущий адрес: {current}. "
            "Возможно, страница не загрузилась, изменилась разметка "
            "или доступ ограничен."
        ) from exc


def save_debug_snapshot(driver: webdriver.Chrome, debug_dir: Path, name: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        path = debug_dir / f"{name}.html"
        path.write_text(driver.page_source, encoding="utf-8")
        LOGGER.warning("HTML для диагностики: %s", path)
    except WebDriverException as exc:
        LOGGER.warning("Не получилось сохранить HTML: %s", exc)
    try:
        path = debug_dir / f"{name}.png"
        if driver.save_screenshot(str(path)):
            LOGGER.warning("Скриншот для диагностики: %s", path)
    except WebDriverException as exc:
        LOGGER.warning("Не получилось сохранить скриншот: %s", exc)


# Рекламные/информационные всплывающие окна Popmechanic могут закрывать
# кнопку подгрузки после нескольких прокруток ленты.
POPUP_SELECTOR = ".popmechanic-content"
POPUP_CLOSE_SELECTORS = (
    "[data-popmechanic-close]",
    ".popmechanic-close",
    ".popmechanic-close-button",
    "[class*='popmechanic-close']",
    ".popmechanic-content [aria-label*='Закры']",
    ".popmechanic-content [aria-label*='Close']",
    ".popmechanic-content [title*='Закры']",
)
LOAD_MORE_SELECTOR = "button.rbc-infinity-scroller-loader"


def popup_visible(driver: webdriver.Chrome) -> bool:
    try:
        return any(
            el.is_displayed()
            for el in driver.find_elements(By.CSS_SELECTOR, POPUP_SELECTOR)
        )
    except (StaleElementReferenceException, WebDriverException):
        return False


def dismiss_popup(driver: webdriver.Chrome) -> bool:
    """Закрывает только видимый Popmechanic стандартной кнопкой интерфейса.

    Если окно не закрывается — не удаляем его из DOM и не нажимаем кнопку
    под ним через JS. Пусть вызывающий код сохранит уже полученные данные.
    """
    if not popup_visible(driver):
        return True

    LOGGER.warning("Появилось окно Popmechanic поверх ленты")
    for selector in POPUP_CLOSE_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for element in elements:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
                try:
                    element.click()
                    WebDriverWait(driver, 3, poll_frequency=0.2).until(
                        lambda d: not popup_visible(d)
                    )
                    LOGGER.info("Окно Popmechanic закрыто")
                    return True
                except (TimeoutException, ElementClickInterceptedException,
                        StaleElementReferenceException, WebDriverException):
                    if not popup_visible(driver):
                        return True
        except (StaleElementReferenceException, WebDriverException):
            continue

    LOGGER.warning("Не нашёл доступную кнопку закрытия Popmechanic")
    return False


def click_load_more(driver: webdriver.Chrome, timeout: int) -> bool:
    """Возвращает False при неустранимом перекрытии кнопки."""
    for attempt in range(1, 4):
        if not dismiss_popup(driver):
            return False
        try:
            button = WebDriverWait(driver, timeout, poll_frequency=0.5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, LOAD_MORE_SELECTOR))
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", button
            )
            button.click()
            return True
        except ElementClickInterceptedException:
            LOGGER.warning(
                "Нажатие 'Загрузить еще' перекрыто, попытка %d/3", attempt
            )
            if not dismiss_popup(driver):
                return False
        except StaleElementReferenceException:
            LOGGER.warning("Кнопка изменилась в DOM, попытка %d/3", attempt)
        except TimeoutException:
            LOGGER.warning("Кнопка загрузки не стала доступной")
            return False
    return False


def save_feed_checkpoint(items: list[NewsArticle], directory: Path) -> Path:
    """Хранит ссылки ДО обхода статей — на случай сбоя при листании."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "feed_checkpoint.json"
    records: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, list):
                records = {
                    row["url"]: row for row in old
                    if isinstance(row, dict) and row.get("url")
                }
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Не удалось прочитать прежний чекпойнт ленты")
    for item in items:
        records[item.url] = asdict(item)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(list(records.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


class RBCNewsParser:

    @staticmethod
    def parse_feed(html: str) -> list[NewsArticle]:
        """Читает карточки новостей; дополняет их метаданными Next.js JSON.

        JSON с первоначальной лентой используется как резервный источник,
        но данные из динамически подгруженных карточек также сохраняются.
        """
        soup = BeautifulSoup(html, "lxml")
        now = datetime.now(timezone.utc).isoformat()
        results: dict[str, NewsArticle] = {}

        # 1. HTML-карточки; после нажатия «Загрузить еще» они дополняются.
        candidates: list[Tag] = []
        for selector in FEED_LINK_SELECTORS:
            candidates = list(soup.select(selector))
            if candidates:
                break

        for anchor in candidates:
            if anchor.name != "a" or not anchor.get("href"):
                continue
            url = canonical_url(str(anchor["href"]))
            if url in results or not allowed_article_url(url):
                continue

            card = (
                anchor.find_parent("article", class_="info-block-custom-news")
                or anchor.find_parent(class_=re.compile(r"^(?:item__wrap|item|news-feed__item)$"))
                or anchor
            )
            title = first_text(anchor, TITLE_SELECTORS) or normalize_text(anchor.get_text(" ", strip=True))
            if len(title) < 8:
                continue

            category = first_text(card, CATEGORY_SELECTORS)
            if not category:
                for link in card.select("a[href]"):
                    value = normalize_text(link.get_text(" ", strip=True))
                    if value in CATEGORY_NAMES:
                        category = value
                        break

            raw_time = first_text(card, TIME_SELECTORS)
            match = TIME_PATTERN.search(raw_time or "")
            published_time = match.group(0) if match else None

            results[url] = NewsArticle(
                title=title,
                url=url,
                category=category,
                published_time=published_time,
                published_at=None,
                scraped_at=now,
            )

        # 2. Встроенный JSON Next.js содержит полные даты (включая секунды).
        # Он также помогает, когда верстка очередной раз меняется.
        node = soup.select_one("script#__NEXT_DATA__")
        if node:
            try:
                payload = json.loads(node.string or node.get_text())
                initial = payload.get("props", {}).get("pageProps", {}).get("initialFeed", {})
                feed_items = initial.get("items", [])
            except (ValueError, TypeError, AttributeError):
                feed_items = []

            for entry in feed_items:
                if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
                    continue
                url = canonical_url(entry["url"])
                if not allowed_article_url(url):
                    continue

                pub_at = None
                raw_date = entry.get("publishDate")
                if isinstance(raw_date, str):
                    try:
                        pub_at = parsedate_to_datetime(raw_date).isoformat()
                    except (TypeError, ValueError, IndexError):
                        pass

                rubric = entry.get("rubric")
                category = rubric.get("title") if isinstance(rubric, dict) else None
                title = normalize_text(entry.get("header") or entry.get("title"))
                if url in results:
                    article = results[url]
                    article.published_at = pub_at or article.published_at
                    if not article.published_time and pub_at:
                        article.published_time = datetime.fromisoformat(pub_at).strftime("%H:%M")
                    article.category = article.category or category
                elif len(title) >= 8:
                    results[url] = NewsArticle(
                        title=title,
                        url=url,
                        category=category,
                        published_time=datetime.fromisoformat(pub_at).strftime("%H:%M") if pub_at else None,
                        published_at=pub_at,
                        scraped_at=now,
                    )

        return list(results.values())

    @staticmethod
    def parse_article(html: str, item: NewsArticle) -> NewsArticle:
        soup = BeautifulSoup(html, "lxml")

        title = first_text(soup, ("h1.article__header__title", "h1",))
        if title:
            item.title = title

        section = _metadata(soup, ('meta[property="article:section"]',))
        if section and not item.category:
            item.category = section

        item.published_at = _extract_time(soup) or item.published_at
        structured = list(_jsonld_articles(soup))
        if not item.published_at:
            for article in structured:
                value = article.get("datePublished")
                if isinstance(value, str) and value.strip():
                    item.published_at = value.strip()
                    break

        body: str | None = None
        for selector in CONTENT_SELECTORS:
            for container in soup.select(selector):
                content = _extract_paragraphs(container)
                if content and (body is None or len(content) > len(body)):
                    body = content
            if body:
                break

        if not body:
            for article in structured:
                value = article.get("articleBody")
                if isinstance(value, str) and value.strip():
                    extracted = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
                    if len(extracted) >= 30:
                        body = normalize_text(extracted)
                        break

        item.content = body
        item.content_status = "ok" if body else "unavailable"
        item.scraped_at = datetime.now(timezone.utc).isoformat()
        return item

    @classmethod
    def collect(
        cls,
        limit: int = 20,
        more: int = 0,
        headless: bool = True,
        delay: float = 2.0,
        timeout: int = 40,
        debug_dir: Path | None = None,
        checkpoint_dir: Path | None = None,
        checkpoint_every: int = 25,
    ) -> list[NewsArticle]:

        if limit < 1 or more < 0 or delay < 0 or checkpoint_every < 1:
            raise ValueError("limit >= 1, more >= 0, delay >= 0 и checkpoint_every >= 1")

        options = Options()
        options.page_load_strategy = "none"
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1365,1000")
        options.add_argument("--lang=ru-RU")
        options.add_argument("--disable-notifications")

        debug_dir = debug_dir or Path(__file__).resolve().parent / "data" / "debug"
        driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(timeout)
            try:
                load_page(
                    driver,
                    FEED_URL,
                    "article.info-block-custom-news a.info-block-title, a.item__link",
                    timeout,
                )
            except (TimeoutException, WebDriverException):
                save_debug_snapshot(driver, debug_dir, "feed_error")
                raise

            # Сохраняем найденные ссылки после каждой удачной подгрузки.
            # Даже если всплывающее окно остановит сбор, эти ссылки останутся.
            if checkpoint_dir is not None:
                save_feed_checkpoint(cls.parse_feed(driver.page_source), checkpoint_dir)

            for page in range(more):
                current_count = len(cls.parse_feed(driver.page_source))
                LOGGER.info(
                    "Лента: %d/%d новостей; подгрузка %d/%d",
                    current_count, limit, page + 1, more,
                )
                if current_count >= limit:
                    break

                # При рекламе или временном перекрытии не падаем с исключением.
                if not click_load_more(driver, timeout):
                    LOGGER.warning(
                        "Подгрузка остановлена на %d новостях: "
                        "кнопка недоступна или перекрыта.", current_count,
                    )
                    save_debug_snapshot(driver, debug_dir, "feed_click_blocked")
                    break

                try:
                    WebDriverWait(driver, timeout).until(
                        lambda d: len(cls.parse_feed(d.page_source)) > current_count
                    )
                except TimeoutException:
                    LOGGER.warning(
                        "После подгрузки %d число новостей не увеличилось", page + 1
                    )
                    save_debug_snapshot(driver, debug_dir, "feed_no_new_items")
                    break

                if checkpoint_dir is not None:
                    checkpoint = save_feed_checkpoint(
                        cls.parse_feed(driver.page_source), checkpoint_dir
                    )
                    LOGGER.info("Ссылки сохранены: %s", checkpoint)
                if delay:
                    time.sleep(delay)

            items = cls.parse_feed(driver.page_source)[:limit]
            if len(items) < limit:
                LOGGER.warning(
                    "В ленте доступно только %d из запрошенных %d новостей. "
                    "Возможно, достигнут конец ленты или исчерпан лимит подгрузок.",
                    len(items), limit,
                )
            if not items:
                save_debug_snapshot(driver, debug_dir, "feed_empty")
                raise RuntimeError("Новости не обнаружены. Проверьте доступ и HTML-селекторы ленты. Снимки в data/debug.")
            LOGGER.info("В ленте получено %d ссылок", len(items))

            result: list[NewsArticle] = []
            for i, item in enumerate(items, start=1):
                try:
                    if i > 1 and delay:
                        time.sleep(delay)
                    load_page(
                        driver,
                        item.url,
                        "h1, [itemprop='articleBody'], meta[property='article:published_time']",
                        timeout,
                    )
                    cls.parse_article(driver.page_source, item)
                    LOGGER.info("[%d/%d] %s — %s", i, len(items), item.content_status, item.title)
                except (TimeoutException, WebDriverException, ValueError) as exc:
                    try:
                        current = urlsplit(driver.current_url)
                        expected = urlsplit(item.url)
                        if current.hostname == expected.hostname and current.path == expected.path:
                            cls.parse_article(driver.page_source, item)
                    except WebDriverException:
                        pass
                    if not item.content:
                        item.content_status = "error"
                        if i <= 3:
                            save_debug_snapshot(driver, debug_dir, f"article_error_{i}")
                    LOGGER.warning("Не удалось полностью обработать %s: %s", item.url, exc)
                result.append(item)
                if checkpoint_dir is not None and (i % checkpoint_every == 0 or i == len(items)):
                    # Промежуточное сохранение: сбой Chrome не потеряет всю работу.
                    save_news(result[-checkpoint_every:], checkpoint_dir)
                    LOGGER.info("Промежуточно сохранено %d/%d статей", i, len(items))
            return result
        finally:
            try:
                driver.quit()
            except WebDriverException as exc:
                LOGGER.warning("Не удалось корректно закрыть Chrome: %s", exc)


def save_news(items: list[NewsArticle], output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = output / "rbc_news.csv", output / "rbc_news.json"
    rows: dict[str, dict[str, Any]] = {}
    if json_path.exists():
        try:
            old = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(old, list):
                rows = {entry["url"]: entry for entry in old if isinstance(entry, dict) and entry.get("url")}
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Старый JSON повреждён: файл будет заменён")

    for item in items:
        record = asdict(item)
        previous = rows.get(item.url)
        if previous and not item.content and previous.get("content"):
            record["content"] = previous["content"]
            record["content_status"] = previous.get("content_status", "ok")
        if previous:
            for key in ("category", "published_time", "published_at"):
                if not record.get(key):
                    record[key] = previous.get(key)
        rows[item.url] = record

    columns = [field.name for field in fields(NewsArticle)]
    records = list(rows.values())
    tmp_json = json_path.with_suffix(".json.tmp")
    tmp_csv = csv_path.with_suffix(".csv.tmp")
    try:
        tmp_json.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        with tmp_csv.open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
        tmp_json.replace(json_path)
        tmp_csv.replace(csv_path)
    finally:
        tmp_json.unlink(missing_ok=True)
        tmp_csv.unlink(missing_ok=True)
    return csv_path, json_path


def main() -> None:

    limit = 1000     # Цель — до 1000 уникальных новостей
    more = 150       # Максимум подгрузок; раньше остановится при достижении limit
    headless = False
    delay = 3.0
    timeout = 40
    output_dir = Path(__file__).resolve().parent / "data"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    articles = RBCNewsParser.collect(
        limit=limit,
        more=more,
        headless=headless,
        delay=delay,
        timeout=timeout,
        debug_dir=output_dir / "debug",
        checkpoint_dir=output_dir,
        checkpoint_every=25,
    )

    csv_path, json_path = save_news(articles, output_dir)
    LOGGER.info("Сохранено %d статей: %s и %s", len(articles), csv_path, json_path)
    LOGGER.info(
        "С текстом: %d; без доступного текста: %d",
        sum(bool(article.content) for article in articles),
        sum(not article.content for article in articles),
    )


if __name__ == "__main__":
    main()
