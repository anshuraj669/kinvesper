"""The fixed phrases Kinvesper speaks on Alexa, in English and Hindi. Everything else (answers,
advice, confirmations) comes from the voice agent, which Claude answers in the person's language.
"""

from __future__ import annotations

LANGUAGES = {"en": "English", "hi": "Hindi"}

TEXT = {
    "en": {
        "welcome": ("Hi {name}, it's Kinvesper. You can ask about your pills, tell me you took them, check a suspicious "
                    "call, or send {caregiver} a message.{anything} What would you like?"),
        "help": ("You can say things like: did I take my pills, I just took my {medicine}, someone called asking for "
                 "gift cards, tell {caregiver} I'm fine, or set up my pill reminders.{anything} What would you like?"),
        "anything": " Or ask me anything else.",
        "reprompt": "Anything else?",
        "goodbye": "Goodbye, take care.",
        "still_working": "Let me look into that. Say okay when you'd like the answer.",
        "say_okay": "Say okay to hear the answer.",
        "not_caught": "Sorry, I didn't catch that. Could you say it another way?",
        "cant_do": "Sorry, I can't do that yet.",
        "unreachable": "Sorry, I can't reach Kinvesper right now. Please try again in a minute.",
        "reminders_set": "Done. Every day at {times}, I'll remind you which medicines to take.",
        "reminders_declined": "Okay, no reminders for now. You can say, set up my pill reminders, any time.",
        "reminders_no_meds": "You don't have any medicines saved yet. Your family can add them in Kinvesper's settings.",
        "reminders_still_blocked": "I still can't set reminders. You can allow them for Kinvesper in the Alexa app.",
        "reminders_failed": "Sorry, I couldn't set your reminders just now. Please try again in a minute.",
        "reminders_off": "Okay, I've turned off your pill reminders.",
        "reminders_off_failed": "Sorry, I couldn't turn off the reminders just now.",
        "reminders_unavailable": "Sorry, reminders aren't available here.",
        "reminder": "{name}, it's time for your {medicines}. {notes} When you've taken them, say: Alexa, tell kin vesper I took my pills.",
        "and": "and",
    },
    "hi": {
        "welcome": ("नमस्ते {name}, मैं Kinvesper हूँ। आप अपनी दवाइयों के बारे में पूछ सकते हैं, बता सकते हैं कि दवाई ले ली, "
                    "किसी शक वाली कॉल के बारे में पूछ सकते हैं, या {caregiver} को संदेश भेज सकते हैं।{anything} बताइए, क्या करूँ?"),
        "help": ("आप ऐसे कह सकते हैं: क्या मैंने दवाई ली, मैंने अभी {medicine} ली, किसी ने कॉल करके गिफ्ट कार्ड माँगे, "
                 "{caregiver} को बताओ मैं ठीक हूँ, या मेरी दवाई के रिमाइंडर सेट करो।{anything} बताइए, क्या करूँ?"),
        "anything": " या मुझसे कुछ भी पूछिए।",
        "reprompt": "और कुछ?",
        "goodbye": "अलविदा, अपना ख़याल रखिए।",
        "still_working": "मैं पता कर रहा हूँ। जवाब सुनना हो तो कहिए, ठीक है।",
        "say_okay": "जवाब सुनने के लिए कहिए, ठीक है।",
        "not_caught": "माफ़ कीजिए, मैं समझ नहीं पाया। क्या आप दूसरे तरीके से कह सकते हैं?",
        "cant_do": "माफ़ कीजिए, मैं अभी यह नहीं कर सकता।",
        "unreachable": "माफ़ कीजिए, अभी Kinvesper से जुड़ नहीं पा रहा। एक मिनट बाद फिर कोशिश कीजिए।",
        "reminders_set": "हो गया। मैं रोज़ {times} आपको याद दिलाऊँगा कि कौन सी दवाई लेनी है।",
        "reminders_declined": "ठीक है, अभी कोई रिमाइंडर नहीं। जब चाहें कहिए, मेरी दवाई के रिमाइंडर सेट करो।",
        "reminders_no_meds": "अभी कोई दवाई सेव नहीं है। आपका परिवार Kinvesper की सेटिंग में दवाइयाँ जोड़ सकता है।",
        "reminders_still_blocked": "मुझे अभी भी रिमाइंडर की अनुमति नहीं है। आप Alexa ऐप में Kinvesper को अनुमति दे सकते हैं।",
        "reminders_failed": "माफ़ कीजिए, अभी रिमाइंडर सेट नहीं हो पाए। एक मिनट बाद फिर कोशिश कीजिए।",
        "reminders_off": "ठीक है, मैंने आपके दवाई के रिमाइंडर बंद कर दिए।",
        "reminders_off_failed": "माफ़ कीजिए, अभी रिमाइंडर बंद नहीं हो पाए।",
        "reminders_unavailable": "माफ़ कीजिए, यहाँ रिमाइंडर उपलब्ध नहीं हैं।",
        "reminder": "{name}, आपकी {medicines} लेने का समय हो गया है। {notes} दवाई लेने के बाद कहिए: Alexa, kin vesper से कहो मैंने दवाई ले ली।",
        "and": "और",
    },
}


def lang(locale: str | None) -> str:
    return "hi" if (locale or "").lower().startswith("hi") else "en"


def t(locale: str | None, key: str, **values: str) -> str:
    return TEXT[lang(locale)][key].format(**values)


def hindi_time(hhmm: str) -> str:
    """'08:00' -> 'सुबह 8 बजे', '20:30' -> 'रात 8:30 बजे'."""
    h, m = (int(x) for x in hhmm.split(":"))
    part = "सुबह" if 4 <= h < 12 else "दोपहर" if 12 <= h < 16 else "शाम" if 16 <= h < 20 else "रात"
    h12 = h % 12 or 12
    return f"{part} {h12} बजे" if m == 0 else f"{part} {h12}:{m:02d} बजे"


def joined(items: list[str], locale: str | None) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {t(locale, 'and')} " + items[-1]
