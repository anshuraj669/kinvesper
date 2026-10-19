"""The fixed phrases Kinvesper speaks on Alexa, in English and Hindi. Everything else (answers,
advice, confirmations) comes from the voice agent, which Claude answers in the person's language.
"""

from __future__ import annotations

import re

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
        "link_account": "Kinvesper needs your family's login first. I've put a card in the Alexa app: open it, "
                        "tap Link account, and sign in with the Kinvesper login.",
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
        "oneoff": "{name}, it's time {action}.",
        "alarm": "{name}, this is an important reminder. It's time {action}.",
        "heads_up": "{name}, in 30 minutes it'll be time {action}.",
        "reminder_saved": "Thank you. Your reminder is set on this Echo.",
        "reminder_not_on_echo": "Okay. I've saved the reminder, but without permission this Echo can't ring for it.",
        "checkin_prompt": "Hello {name}, it's Kinvesper checking in. Please let me know you're okay. Just say: Alexa, tell kin vesper I'm fine.",
        "family_message": "{name}, you have a message from {sender}: {text}",
        "family_question": "{name}, {sender} is asking: {text}",
        "family_voice": "{name}, {sender} sent you a voice message.",
        "family_photo": "{name}, {sender} sent you a photo. {text}",
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
        "link_account": "पहले Kinvesper को आपके परिवार का लॉगिन चाहिए। मैंने Alexa ऐप में एक कार्ड भेजा है: उसे खोलिए, "
                        "Link account दबाइए, और Kinvesper लॉगिन से साइन इन कीजिए।",
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
        "oneoff": "{name}, आपका रिमाइंडर: {text}।",
        "alarm": "{name}, यह आपका ज़रूरी रिमाइंडर है: {text}।",
        "heads_up": "{name}, 30 मिनट में: {text}।",
        "reminder_saved": "धन्यवाद। आपका रिमाइंडर इस Echo पर सेट हो गया है।",
        "reminder_not_on_echo": "ठीक है। मैंने रिमाइंडर सेव कर लिया है, पर अनुमति के बिना यह Echo उसके लिए नहीं बजेगा।",
        "checkin_prompt": "नमस्ते {name}, Kinvesper आपका हाल पूछ रहा है। बस कहिए: Alexa, kin vesper से कहो मैं ठीक हूँ।",
        "family_message": "{name}, {sender} का संदेश आया है: {text}",
        "family_question": "{name}, {sender} पूछ रहे हैं: {text}",
        "family_voice": "{name}, {sender} ने आपको आवाज़ में संदेश भेजा है।",
        "family_photo": "{name}, {sender} ने आपको एक फ़ोटो भेजी है। {text}",
    },
}


# Spoken reminders say "it's time to drink water" or "it's time for your doctor's appointment".
VERBS = set("""take drink call eat go check pay water feed walk buy pick turn switch lock unlock close open send book
meet see visit watch cook clean wash charge sleep wake get bring collect put read write text message ring phone dial
exercise stretch pray do start stop leave join attend prepare pack renew order fill apply change move have give tell
ask remember sign submit wish greet measure test record wear use bathe shower brush comb boil heat cool mail post
return fetch catch drop make finish rest sit stand lie play sing listen""".split())
NOT_ACTIONS = {"the", "my", "your", "a", "an", "his", "her", "our", "their", "this", "that"}


PERSON = {"my": "your", "me": "you", "i": "you", "myself": "yourself", "mine": "yours", "i'm": "you're"}


def action(text: str) -> str:
    """'drink water' -> 'to drink water'; "doctor's appointment" -> "for your doctor's appointment";
    'take my medicine' -> 'to take your medicine' (it's said back to the person)."""
    text = re.sub(r"\bI am\b", "you are", text, flags=re.I)
    text = re.sub(r"\b(?:my|me|i|myself|mine|i'm)\b", lambda m: PERSON[m[0].lower()], text, flags=re.I)
    first = text.split()[0].lower() if text.split() else ""
    if first in VERBS:
        return f"to {text}"
    if first in NOT_ACTIONS:
        return f"for {text}"
    return f"for your {text}"


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
