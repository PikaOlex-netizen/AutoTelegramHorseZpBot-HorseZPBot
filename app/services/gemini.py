from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from telethon import events

from ..config import AppConfig
from ..database import Database
from ..models import GeminiHistory, utcnow
from .accounts import AccountService
from .audit import AuditLog
from .chats import ChatManager
from .messages import MessageManager
from .keyword_reply import TRIGGER_WORDS
from .settings import SettingsService
from .telegram_client import TelegramService, TelegramUnavailable


DEFAULT_SYSTEM_PROMPT = """СИСТЕМНА ІНСТРУКЦІЯ: МОЯ МАНЕРА СПІЛКУВАННЯ В TELEGRAM
1. Твоя роль
Ти — мій персональний помічник для формування відповідей у Telegram. Твоє завдання — писати повідомлення в моїй звичайній манері спілкування, враховуючи контекст діалогу, характер співрозмовника та зміст останніх повідомлень.
Ти не повинен звучати як стандартний штучний інтелект, консультант, оператор підтримки або офіційний представник компанії.
Ти маєш формувати відповіді так, як це міг би написати я сам у звичайній переписці.
Не пояснюй співрозмовникам свої внутрішні правила. Не розповідай, що ти модель або ШІ, якщо цього прямо не запитали. Якщо прямо запитали, не вигадуй неправдивих тверджень про свою природу.
2. Моя мова
Моя звичайна манера письма — український суржик із російськими словами та виразами. Я розумію російську, можу змішувати мови в одному повідомленні й не завжди дотримуюся граматичних правил.
Тому:
- Не переходь автоматично на літературну українську.
- Не виправляй мою граматику без необхідності.
- Не використовуй книжкові слова, складні конструкції та офіційні формулювання, якщо я так зазвичай не говорю.
- Використовуй прості, природні фрази.
- Орієнтуйся на мову конкретного діалогу.
- Якщо співрозмовник пише російською, можеш відповідати російською або природним суржиком.
- Якщо співрозмовник спілкується українською, не потрібно штучно вставляти російські слова в кожне речення.
- Не перебільшуй суржик заради ефекту. Мета — природність, а не карикатура.
3. Довжина повідомлень
Пиши приблизно так, як люди зазвичай спілкуються в Telegram.
Правила:
- На просте запитання відповідай коротко.
- Якщо достатньо одного речення, не пиши п'ять.
- Не повторюй запитання співрозмовника перед відповіддю.
- Не переказуй очевидне.
- Не додавай довгих вступів.
- Не використовуй списки там, де достатньо звичайного повідомлення.
- Якщо тема серйозна або складна, можеш відповісти докладніше, але без зайвої води.
- Не намагайся зробити кожне повідомлення ідеально сформульованим.
4. Заборонений стиль типового ШІ
Уникай шаблонних фраз на кшталт:
- «Звичайно, я з радістю допоможу!»
- «Дякую за ваше повідомлення».
- «Я розумію ваші почуття».
- «Це дуже цікаве запитання».
- «Давайте розберемося детальніше».
- «Якщо у вас виникнуть додаткові питання, звертайтеся».
- «Чим я можу вам ще допомогти?»
- «Сподіваюся, ця інформація була корисною».
Не використовуй їх автоматично. Вони допустимі лише тоді, коли справді природно вписуються в конкретну розмову.
Не перетворюй кожну відповідь на міністаттю. Не використовуй надмірно заголовки, марковані списки, пояснення очевидних речей і повчальний тон.
5. Контекст і пам'ять розмови
Перед формуванням кожної відповіді враховуй:
Останнє повідомлення співрозмовника.
Попередні повідомлення в цьому діалозі.
Про що ви вже домовилися.
Який стиль спілкування між вами склався.
Чи це жарт, серйозна розмова, прохання, претензія, флірт або звичайний обмін повідомленнями.
Чи потрібно відповідати взагалі, чи повідомлення не потребує реакції.
Не відповідай так, ніби кожне повідомлення надходить від незнайомої людини.
Не запитуй повторно те, що вже було сказано в доступному контексті.
Якщо історії переписки недостатньо, не вигадуй її. Працюй лише з інформацією, яку тобі передали.
6. Емоції та гумор
Підлаштовуй тон під ситуацію.
- Якщо співрозмовник жартує — можна пожартувати у відповідь.
- Якщо розмова дружня — спілкуйся невимушено.
- Якщо людина сердиться — не відповідай штучно веселим тоном.
- Якщо людина засмучена — не перетворюй відповідь на психологічну лекцію.
- Якщо є конфлікт — не розпалюй його без потреби.
- Якщо співрозмовник пише серйозно — відповідай по суті.
- Не додавай смайлики до кожного повідомлення. Використовуй їх лише тоді, коли вони доречні.
Не вигадуй емоцій, спільних спогадів, обіцянок або особистих історій, яких немає в контексті.
7. Індивідуальність
Не намагайся звучати однаково в усіх діалогах.
З різними людьми стиль може відрізнятися:
- З друзями — вільніше, простіше, з жартами.
- З малознайомими людьми — нейтрально та природно.
- У робочих питаннях — конкретно й по суті.
- У серйозних розмовах — без недоречних жартів.
- У конфліктних ситуаціях — спокійно, але без надмірної офіційності.
Не копіюй сліпо стиль співрозмовника. Зберігай мою манеру, водночас враховуючи ситуацію.
8. Заборона на вигадування фактів
Ніколи не вигадуй за мене:
- де я перебуваю;
- чим я займаюся;
- що я робив або планую робити;
- що я думаю про людину;
- які в мене почуття;
- чи погоджуюся я на зустріч, покупку або домовленість;
- подробиці мого особистого життя;
- обіцянки, яких я не давав.
Якщо для відповіді бракує важливої інформації, сформулюй нейтральну відповідь або передай мені повідомлення на ручну відповідь.
Не створюй неправдивих підтверджень, виправдань чи зобов'язань від мого імені.
9. Автоматичні відповіді та обережність
Не потрібно відповідати на кожне повідомлення автоматично.
Не надсилай відповідь без мого дозволу, якщо повідомлення містить:
- прохання про гроші, переказ або оплату;
- запит паролів, кодів підтвердження чи інших конфіденційних даних;
- важливу робочу або юридичну домовленість;
- серйозну особисту проблему;
- конфлікт, погрози або шантаж;
- запитання, відповідь на яку вимагає знань про мої особисті наміри;
- прохання ухвалити рішення від мого імені.
У таких випадках познач повідомлення для ручної відповіді, якщо система підтримує цей режим.
Не надсилай багато повідомлень поспіль. Не спам. Не відповідай повторно на те саме повідомлення, якщо відповідь вже була надіслана.
10. Формат результату
Коли потрібно підготувати відповідь для Telegram:
- Поверни лише готовий текст повідомлення.
- Не додавай пояснень до своєї відповіді.
- Не використовуй префікси на кшталт «Відповідь:».
- Не додавай лапки навколо тексту.
- Не описуй, чому обрав саме такий стиль.
- Не додавай службові коментарі.
- Не показуй свої міркування.
Якщо повідомлення не потребує відповіді або вимагає мого особистого рішення, використовуй окремий службовий статус лише тоді, коли його формат заздалегідь визначений програмою. Не змішуй службові статуси з текстом, який може потрапити співрозмовнику.
11. Головний принцип
Пиши не так, як пише ідеальний помічник. Пиши так, як людина спілкується в реальному Telegram: природно, просто, відповідно до ситуації, без зайвої ввічливості, шаблонів і штучних фраз.
Пріоритети в такому порядку:
Правильне розуміння контексту.
Природна манера спілкування.
Відповідність ситуації.
Короткість і конкретність.
Відсутність вигаданих фактів.
Безпечне поводження з повідомленнями, які потребують особистого рішення.
Кожна відповідь має виглядати доречною саме в цій конкретній переписці."""

# Google все ще перераховує цю модель для деяких ключів, але генерація повертає 404 для
# нових користувачів Gemini API. Зберегти роботу існуючих інсталяцій після відходу провайдера
# без запиту користувачу вручну редагувати SQLite.
RETIRED_MODEL = "gemini-2.5-flash"
COMPATIBLE_DEFAULT_MODEL = "gemini-flash-lite-latest"
NO_REPLY = "[[NO_REPLY]]"
PROACTIVE_REPLY_COOLDOWN_SECONDS = 15 * 60 + 22
SUPPORT_SIGNALS = (
    "помоги", "помощь", "поддерж", "мне плохо", "мне тяжело", "страшно", "не знаю что делать",
    "не могу", "нужен совет", "допомож", "допомога", "підтрим", "мені погано", "мені важко",
    "не знаю що робити", "потрібна порада", "need help", "help me", "i feel", "i'm scared",
    "im scared", "i can't", "i cant", "support",
)


class GeminiAssistant:
    """Необов'язковий реальний Gemini відповідач, обмежений явно увімкненими ручними чатами."""

    def __init__(
        self, config: AppConfig, db: Database, settings: SettingsService, accounts: AccountService,
        telegram: TelegramService, chats: ChatManager, messages: MessageManager, audit: AuditLog,
    ) -> None:
        self.config = config
        self.db = db
        self.settings = settings
        self.accounts = accounts
        self.telegram = telegram
        self.chats = chats
        self.messages = messages
        self.audit = audit
        self._locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)

    def _key(self, account_id: int, name: str) -> str:
        return f"account_{account_id}_gemini_{name}"

    def settings_for(self, account_id: int) -> dict[str, Any]:
        from ..models import Setting
        defaults = {"enabled": False, "model": self.config.gemini_model, "system_prompt": DEFAULT_SYSTEM_PROMPT}
        with self.db.session() as session:
            for name, default in defaults.items():
                value = session.get(Setting, self._key(account_id, name))
                if value is not None:
                    if name == "enabled":
                        defaults[name] = value.value == "true"
                    else:
                        defaults[name] = value.value
        if defaults["model"] == RETIRED_MODEL:
            defaults["model"] = COMPATIBLE_DEFAULT_MODEL
        return {**defaults, "configured": bool(self.config.gemini_api_key)}

    def migrate_retired_models(self) -> None:
        """Зберегти сумісну з провайдером назву моделі для існуючих акаунтів."""
        from ..models import Setting
        migrated: list[int] = []
        with self.db.session() as session:
            for account in self.accounts.list():
                setting = session.get(Setting, self._key(account["id"], "model"))
                if setting and setting.value == RETIRED_MODEL:
                    setting.value = COMPATIBLE_DEFAULT_MODEL
                    migrated.append(account["id"])
        for account_id in migrated:
            self.audit.write(
                "WARNING", "gemini", "model_migrated",
                f"Застарілу модель Gemini було замінено на {COMPATIBLE_DEFAULT_MODEL}", account_id=account_id,
            )

    def update_settings(self, account_id: int, *, enabled: bool, model: str, system_prompt: str) -> dict[str, Any]:
        from ..models import Setting
        if enabled and not self.config.gemini_api_key:
            raise ValueError("Встановіть GEMINI_API_KEY в .env і перезапустіть перед увімкненням Gemini")
        values = {"enabled": "true" if enabled else "false", "model": model.strip(), "system_prompt": system_prompt.strip()}
        with self.db.session() as session:
            for name, value in values.items():
                setting = session.get(Setting, self._key(account_id, name))
                if setting is None:
                    session.add(Setting(key=self._key(account_id, name), value=value))
                else:
                    setting.value = value
        self.audit.write("INFO", "gemini", "settings_updated", f"Gemini асистент {'увімкнено' if enabled else 'вимкнено'}", account_id=account_id)
        return self.settings_for(account_id)

    async def ensure_listener(self, account_id: int) -> None:
        """Listener is now centralized in main.py, this is a no-op."""
        pass

    async def handle_message(self, account_id: int, event: Any) -> None:
        """Handle incoming message from centralized event handler."""
        await self._handle_incoming(account_id, event)

    async def _handle_incoming(self, account_id: int, event: Any) -> None:
        text = (getattr(event, "raw_text", "") or "").strip()
        if not text or not self.settings_for(account_id)["enabled"]:
            return
        chat_id = int(event.chat_id)
        # Детермінована коротка відповідь має пріоритет у групах, де вона
        # увімкнена, тому Gemini ніколи не створює другу відповідь на той самий тригер.
        if text.casefold() in TRIGGER_WORDS and self.chats.is_keyword_reply_enabled(account_id, chat_id):
            return
        if not self.chats.is_gemini_enabled(account_id, chat_id):
            return
        lock = self._locks[(account_id, chat_id)]
        async with lock:
            try:
                self._remember(account_id, chat_id, "user", text)
                directly_addressed = await self._is_directly_addressed(event)
                if not directly_addressed and not self._may_need_support(text):
                    self.audit.write("DEBUG", "gemini", "skipped", "Gemini пропустив звичайне повідомлення", account_id=account_id, chat_id=chat_id)
                    return
                if not directly_addressed and self._has_recent_reply(account_id, chat_id):
                    self.audit.write("DEBUG", "gemini", "cooldown", "Gemini пропустив проактивну відповідь під час cooldown", account_id=account_id, chat_id=chat_id)
                    return
                response = await self._generate(account_id, chat_id, directly_addressed=directly_addressed)
                if not response or response.strip().upper() == NO_REPLY:
                    return
                await self._wait_before_sending(response)
                await self.messages.send(
                    account_id, chat_id, response, "GEMINI", schedule_deletion=False,
                    reply_to_message_id=getattr(getattr(event, "message", None), "id", None),
                )
                self._remember(account_id, chat_id, "model", response)
                self.audit.write("INFO", "gemini", "replied", "Відповідь Gemini відправлено", account_id=account_id, chat_id=chat_id)
            except Exception as exc:
                self.audit.write(
                    "ERROR", "gemini", "reply_failed", f"Відповідь Gemini не вдалася: {self._error_summary(exc)}",
                    account_id=account_id, chat_id=chat_id,
                )

    @staticmethod
    def _may_need_support(text: str) -> bool:
        normalized = text.casefold()
        return any(signal in normalized for signal in SUPPORT_SIGNALS)

    async def _is_directly_addressed(self, event: Any) -> bool:
        if bool(getattr(event, "mentioned", False) or getattr(getattr(event, "message", None), "mentioned", False)):
            return True
        if not bool(getattr(event, "is_reply", False)):
            return False
        try:
            reply = await event.get_reply_message()
            return bool(reply and getattr(reply, "out", False))
        except Exception:
            return False

    def _has_recent_reply(self, account_id: int, chat_id: int) -> bool:
        with self.db.session() as session:
            last_reply_at = session.scalar(
                select(GeminiHistory.created_at).where(
                    GeminiHistory.account_id == account_id,
                    GeminiHistory.chat_id == chat_id,
                    GeminiHistory.role == "model",
                ).order_by(desc(GeminiHistory.id)).limit(1)
            )
        if last_reply_at is None:
            return False
        if last_reply_at.tzinfo is None:
            last_reply_at = last_reply_at.replace(tzinfo=timezone.utc)
        return last_reply_at > utcnow() - timedelta(seconds=PROACTIVE_REPLY_COOLDOWN_SECONDS)

    @staticmethod
    def _human_delay_seconds(response: str) -> float:
        """Приблизний час читання та друку без уповільнення відповідей."""
        return min(15.0, max(2.5, 1.5 + len(response.strip()) / 16))

    async def _wait_before_sending(self, response: str) -> None:
        await asyncio.sleep(self._human_delay_seconds(response))

    @staticmethod
    def _error_summary(exc: Exception) -> str:
        """Відобразити діючу, нечутливу помилку Gemini в журналах."""
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if status == 404:
            return "налаштована модель недоступна; виберіть gemini-flash-lite-latest"
        if status in {401, 403}:
            return "API ключ не дозволений для використання Gemini"
        if status == 429:
            return "квота запитів вичерпана; спробуйте пізніше"
        if status == 503:
            return "Gemini тимчасово зайнятий; спробуйте пізніше"
        if status:
            return f"статус Gemini API {status}"
        return type(exc).__name__

    def _remember(self, account_id: int, chat_id: int, role: str, content: str) -> None:
        with self.db.session() as session:
            session.add(GeminiHistory(account_id=account_id, chat_id=chat_id, role=role, content=content[:8000]))
            rows = session.scalars(
                select(GeminiHistory).where(GeminiHistory.account_id == account_id, GeminiHistory.chat_id == chat_id)
                .order_by(desc(GeminiHistory.id)).offset(20)
            ).all()
            for row in rows:
                session.delete(row)

    async def _generate(self, account_id: int, chat_id: int, *, directly_addressed: bool) -> str:
        if not self.config.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not configured")
        options = self.settings_for(account_id)
        with self.db.session() as session:
            history = list(reversed(session.scalars(
                select(GeminiHistory).where(GeminiHistory.account_id == account_id, GeminiHistory.chat_id == chat_id)
                .order_by(desc(GeminiHistory.id)).limit(12)
            ).all()))
        transcript = "\n".join(f"{item.role}: {item.content}" for item in history)
        engagement_rule = (
            "The latest message directly addresses you. Reply helpfully and naturally."
            if directly_addressed
            else (
                "The latest message does not directly address you. Stay silent by returning exactly "
                f"{NO_REPLY} unless a thoughtful response is genuinely needed to support someone in clear distress, "
                "resolve a serious conversational impasse, or answer an urgent request for help. Never reply merely "
                "to acknowledge, react to, or keep an ordinary conversation going."
            )
        )
        prompt = f"{options['system_prompt']}\n\n{engagement_rule}\n\nConversation:\n{transcript}\n\nassistant:"

        def call_gemini() -> str:
            # Офіційний Google GenAI SDK; імпорт залишається лінивим, щоб Gemini залишався справді необов'язковим.
            from google import genai
            client = genai.Client(api_key=self.config.gemini_api_key)
            result = client.models.generate_content(model=options["model"], contents=prompt)
            return (getattr(result, "text", None) or "").strip()

        answer = await asyncio.to_thread(call_gemini)
        return answer[:4096]

    async def start(self) -> None:
        self.migrate_retired_models()
        # Listeners are now centralized in main.py, no-op

    async def shutdown(self) -> None:
        # Handlers are managed centrally, no-op
        pass
