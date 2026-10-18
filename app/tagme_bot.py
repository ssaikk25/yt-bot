"""Автоматизация TagMe через Playwright (persistent context).

Принципы надёжности:
- Перед каждым действием страница приводится в готовность (`ensure_ready`).
- Локаторы создаются заново при каждом обращении (Playwright сам бросает
  ошибку "element is not attached", если элемент пересоздан после reload).
- Клавиши — основной путь, клик — fallback.
- После каждой операции проверяется результат (значение поля, checked radio,
  сообщение результата, подтверждение отправки).
- Перезагрузка страницы (~раз в 20 минут) перехватывается событиями навигации,
  и работа продолжается с текущего кандидата без потери очереди.

Селекторы вынесены в константы в начале файла — при изменении вёрстки TagMe
достаточно поправить их (или включить дамп DOM для отладки).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

from .logger import get_logger

log = get_logger("tagme")

# ---------------------------------------------------------------------------
# Тексты результатов (поправьте, если в реальном UI они отличаются).
# ---------------------------------------------------------------------------
ALREADY_EXISTS = "Такая ссылка уже есть в базе"
NOT_EXISTS = "Такой ссылки нет, можно отправлять"

# Тексты подтверждения отправки (эвристики; см. README).
SEND_CONFIRMATION_TEXTS = [
    "Отправлено",
    "Успешно",
    "Спасибо",
    "Добавлено",
]

# ---------------------------------------------------------------------------
# Селекторы (список — fallback по порядку).
# ---------------------------------------------------------------------------
INPUT_SELECTORS = [
    "input[type='url']",
    "input[placeholder*='youtube' i]",
    "input[placeholder*='ссылк' i]",
    "input[placeholder*='URL' i]",
    "input[placeholder*='http' i]",
]

CHECK_BUTTON_SELECTORS = [
    "button:has-text('Проверить ссылку')",
    "button:has-text('Проверить')",
    "input[type='button'][value*='Проверить' i]",
]

SEND_BUTTON_SELECTORS = [
    "button:has-text('Отправить')",
    "button[type='submit']",
    "input[type='submit']",
]


class TagMeBot:
    def __init__(self, settings):
        self.settings = settings
        self._playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._navigated = False

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        pw = await async_playwright().start()
        self._playwright = pw
        profile = Path(self.settings.playwright_profile_dir)
        profile.mkdir(parents=True, exist_ok=True)

        self.context = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=self.settings.headless,
            viewport={"width": 1366, "height": 900},
            slow_mo=self.settings.slow_mo_ms,
            args=["--no-sandbox"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("framenavigated", self._on_navigation)
        self.page.on("load", self._on_load)

        await self.page.goto(
            self.settings.tagme_url,
            wait_until="domcontentloaded",
            timeout=self.settings.tagme_ready_timeout_ms,
        )
        await self.ensure_ready()
        log.info("TagMe: страница загружена и готова")

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        finally:
            if self._playwright:
                await self._playwright.stop()
            self._playwright = None
            self.context = None
            self.page = None

    def _on_navigation(self, *args) -> None:
        self._navigated = True
        log.info("TagMe: обнаружена навигация/перезагрузка страницы")

    def _on_load(self, *args) -> None:
        log.debug("TagMe: load event")

    # ------------------------------------------------------------------ #
    # Готовность страницы
    # ------------------------------------------------------------------ #
    async def ensure_ready(self) -> None:
        """Дожидается появления поля ввода и кнопки проверки (в т.ч. после reload)."""
        if self._navigated:
            log.info("TagMe: страница перезагрузилась, ожидаю готовность...")
            self._navigated = False
        try:
            await self._find_input(timeout_ms=self.settings.tagme_ready_timeout_ms)
            await self._find_check_button(timeout_ms=self.settings.tagme_ready_timeout_ms)
        except RuntimeError as e:
            await self._dump_dom("not_ready")
            raise RuntimeError("TagMe не загрузился: поле/кнопка не появились") from e

    # ------------------------------------------------------------------ #
    # Поиск элементов (свежие локаторы при каждом вызове + ожидание)
    # ------------------------------------------------------------------ #
    async def _first_visible(self, selectors: list[str], description: str, timeout_ms: int) -> Locator:
        """Ждёт появления первого видимого элемента из списка селекторов.

        Опрос с коротким интервалом вместо однократной проверки — так мы корректно
        переживаем перезагрузку страницы (элементы появляются заново).
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            for sel in selectors:
                try:
                    loc = self.page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        return loc
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Таймаут ожидания элемента: {description}")
            await asyncio.sleep(0.25)

    async def _find_input(self, timeout_ms: Optional[int] = None) -> Locator:
        return await self._first_visible(
            INPUT_SELECTORS, "поле ввода URL",
            timeout_ms if timeout_ms is not None else self.settings.tagme_ready_timeout_ms,
        )

    async def _find_check_button(self, timeout_ms: Optional[int] = None) -> Locator:
        return await self._first_visible(
            CHECK_BUTTON_SELECTORS, "кнопка 'Проверить ссылку'",
            timeout_ms if timeout_ms is not None else self.settings.tagme_timeout_ms,
        )

    async def _find_send_button(self, timeout_ms: Optional[int] = None) -> Locator:
        return await self._first_visible(
            SEND_BUTTON_SELECTORS, "кнопка 'Отправить'",
            timeout_ms if timeout_ms is not None else self.settings.tagme_timeout_ms,
        )

    # ------------------------------------------------------------------ #
    # Полный сценарий для одного канала
    # ------------------------------------------------------------------ #
    async def submit_channel(self, channel_url: str, category_key: str, category_label: str) -> str:
        """Возвращает: 'exists' | 'sent' | 'skipped'. Бросает исключение при фатальных ошибках."""
        result = await self.check_link(channel_url)
        if result == ALREADY_EXISTS:
            return "exists"
        if result != NOT_EXISTS:
            return "skipped"

        # Ссылки нет — оформляем отправку.
        await self.select_channel()
        await self.select_category(category_key, category_label)
        confirmed = await self.submit()
        return "sent" if confirmed else "skipped"

    # ------------------------------------------------------------------ #
    # Шаг 1: проверка ссылки
    # ------------------------------------------------------------------ #
    async def check_link(self, channel_url: str) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                await self.ensure_ready()
                inp = await self._find_input()
                await inp.fill(channel_url)
                await self._verify_field_value(inp, channel_url)

                check = await self._find_check_button()
                await check.click()
                return await self._wait_for_result()
            except PWTimeoutError as e:
                last_error = e
                log.warning("TagMe: таймаут проверки ссылки, повтор %d", attempt + 1)
            except Exception as e:
                last_error = e
                log.warning("TagMe: ошибка проверки ссылки (%s), повтор %d", type(e).__name__, attempt + 1)
                # Если страница перезагрузилась — ensure_ready в следующей итерации восстановит.
        raise RuntimeError(f"Не удалось проверить ссылку на TagMe: {last_error}")

    async def _verify_field_value(self, inp: Locator, url: str) -> None:
        try:
            value = (await inp.input_value()).strip()
        except Exception:
            value = ""
        if value != url.strip():
            log.warning("TagMe: значение поля не совпадает с ожидаемым (в поле: %r)", value)
            try:
                await inp.fill(url)
            except Exception:
                pass

    async def _wait_for_result(self) -> str:
        try:
            await self.page.get_by_text(ALREADY_EXISTS, exact=False).first.wait_for(
                state="visible", timeout=self.settings.tagme_timeout_ms
            )
            return ALREADY_EXISTS
        except PWTimeoutError:
            pass

        try:
            await self.page.get_by_text(NOT_EXISTS, exact=False).first.wait_for(
                state="visible", timeout=self.settings.tagme_timeout_ms
            )
            return NOT_EXISTS
        except PWTimeoutError:
            # Fallback: поиск по содержимому страницы.
            content = await self.page.content()
            if ALREADY_EXISTS in content:
                return ALREADY_EXISTS
            if NOT_EXISTS in content:
                return NOT_EXISTS
            raise RuntimeError("Сообщение результата проверки не появилось")

    # ------------------------------------------------------------------ #
    # Шаг 2: выбор «Канал» клавишей 1
    # ------------------------------------------------------------------ #
    async def select_channel(self) -> None:
        await self.ensure_ready()
        try:
            await self.page.keyboard.press("1")
        except Exception as e:
            log.warning("TagMe: клавиша '1' не нажалась: %s", e)

        if await self._channel_selected():
            log.info("TagMe: выбран тип 'Канал'")
            return

        # Fallback: клик по radio/label «Канал».
        try:
            loc = self.page.locator("label", has_text="Канал").first
            if await loc.count() > 0:
                await loc.click()
        except Exception as e:
            log.warning("TagMe: fallback клик по 'Канал' не сработал: %s", e)

        if not await self._channel_selected():
            log.warning("TagMe: не удалось подтвердить выбор 'Канал' (продолжаю)")
        else:
            log.info("TagMe: выбран тип 'Канал' (fallback клик)")

    async def _channel_selected(self) -> bool:
        """Пытается определить, выбран ли radio «Канал»."""
        try:
            # label с текстом «Канал» и вложенным radio
            labels = self.page.locator("label", has_text="Канал")
            for i in range(await labels.count()):
                label = labels.nth(i)
                radio = label.locator("input[type='radio']")
                if await radio.count() > 0:
                    return bool(await radio.is_checked())
            # radio, чей value или id содержит 'channel'
            radios = self.page.locator("input[type='radio']")
            for i in range(await radios.count()):
                r = radios.nth(i)
                val = ((await r.get_attribute("value")) or "").lower()
                rid = ((await r.get_attribute("id")) or "").lower()
                if "channel" in val or "channel" in rid:
                    return bool(await r.is_checked())
        except Exception:
            return False
        return False

    # ------------------------------------------------------------------ #
    # Шаг 3: выбор категории клавишей
    # ------------------------------------------------------------------ #
    async def select_category(self, key: str, label: str) -> None:
        await self.ensure_ready()
        try:
            await self.page.keyboard.press(key)
        except Exception as e:
            log.warning("TagMe: клавиша категории %r не нажалась: %s", key, e)

        if await self._category_selected(label):
            log.info("TagMe: выбрана категория %r (клавиша %r)", label, key)
            return

        # Fallback: клик по label категории.
        try:
            await self.page.locator("label", has_text=label).first.click()
        except Exception as e:
            log.warning("TagMe: fallback клик по категории %r не сработал: %s", label, e)

        if await self._category_selected(label):
            log.info("TagMe: выбрана категория %r (fallback клик)", label)
        else:
            log.warning("TagMe: не удалось подтвердить выбор категории %r", label)

    async def _category_selected(self, label: str) -> bool:
        try:
            labels = self.page.locator("label", has_text=label)
            for i in range(await labels.count()):
                lbl = labels.nth(i)
                radio = lbl.locator("input[type='radio']")
                if await radio.count() > 0:
                    return bool(await radio.is_checked())
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Шаг 4: отправка
    # ------------------------------------------------------------------ #
    async def submit(self) -> bool:
        await self.ensure_ready()
        if self.settings.dry_run:
            log.info("DRY_RUN: кнопка 'Отправить' НЕ нажимается")
            return True

        btn = await self._find_send_button()
        await btn.click()
        log.info("TagMe: нажата кнопка 'Отправить'")

        if await self._wait_send_confirmation():
            log.info("TagMe: отправка подтверждена")
            return True

        # Подтверждения нет. Если была перезагрузка — проверим статус ссылки,
        # но НЕ будем отправлять повторно (защита от дублей).
        if self._navigated:
            log.warning("TagMe: перезагрузка во время отправки; отправка не подтверждена (без повторной отправки)")
        else:
            log.warning("TagMe: подтверждение отправки не найдено")
        return False

    async def _wait_send_confirmation(self) -> bool:
        for text in SEND_CONFIRMATION_TEXTS:
            try:
                await self.page.get_by_text(text, exact=False).first.wait_for(
                    state="visible", timeout=self.settings.tagme_send_timeout_ms
                )
                return True
            except PWTimeoutError:
                continue
        # Эвристика: после успешной отправки ссылка может сразу стать «уже есть в базе».
        try:
            await self.page.get_by_text(ALREADY_EXISTS, exact=False).first.wait_for(
                state="visible", timeout=2000
            )
            return True
        except PWTimeoutError:
            return False

    # ------------------------------------------------------------------ #
    # Отладка
    # ------------------------------------------------------------------ #
    async def _dump_dom(self, prefix: str) -> None:
        """Сохраняет HTML и структуру input/button/label для настройки селекторов."""
        try:
            dump_dir = Path(self.settings.temp_dir) / "tagme_dump"
            dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = int(time.time())
            html = await self.page.content()
            (dump_dir / f"{prefix}_{stamp}.html").write_text(html, encoding="utf-8")

            info = await self.page.evaluate(
                """() => {
                    const inputs = [...document.querySelectorAll('input')].map(i => ({
                        type: i.type, value: i.value, placeholder: i.placeholder,
                        checked: i.checked, id: i.id, name: i.name
                    }));
                    const buttons = [...document.querySelectorAll('button')].map(b => ({
                        text: (b.innerText || '').trim().slice(0, 80), type: b.type
                    }));
                    const labels = [...document.querySelectorAll('label')].map(l => ({
                        text: (l.innerText || '').trim().slice(0, 80)
                    }));
                    return {url: location.href, inputs, buttons, labels};
                }"""
            )
            (dump_dir / f"{prefix}_{stamp}.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("TagMe: DOM-дамп сохранён в %s", dump_dir)
        except Exception as e:
            log.warning("TagMe: не удалось сохранить DOM-дамп: %s", e)
