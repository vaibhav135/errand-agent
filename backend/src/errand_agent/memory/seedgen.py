"""Seed generator: a lived-in demo user, built deterministically.

What: `build_seed()` returns the demo user's profile, 55 memories and ~200 messages
      across 10 past sessions; `python -m errand_agent.memory.seedgen` (make seed-data)
      writes it to memory/data/demo_user.json, which is committed.

Why:  The assignment seeds "at least 50 stored memories and a long prior conversation"
      to test context management. Hand-writing 50+ memories and 200 messages is
      error-prone; generating them from hand-written key facts plus templates keeps
      the important cases deliberate and the volume cheap, and a fixed seed keeps the
      output identical every run (a test compares it with the committed file).

How:  - Key facts are written by hand and cover every state the context builder must
        handle: explicit active, superseded chains, forgotten tombstones, pending,
        rejected and confirmed inferences. ~30 irrelevant notes (food, travel, work,
        family) come from templates.
      - The data follows our own rules: each explicit memory's quote appears in a seeded
        user message; forgotten facts appear only as "[forgotten]"; the profile intro
        reads "[phone saved]" etc., as if scrubbing had already run.
      - The profile is fictional: example.com is reserved for documentation.
"""

import json
import random
from pathlib import Path
from typing import Any

SEED = 7
SESSIONS = 10
MESSAGES_PER_SESSION = 20
OUTPUT = Path(__file__).parent / "data" / "demo_user.json"
DEMO_USER_ID = "user_1"  # the mock world's default user, so seeded bookings line up

PROFILE = {
    "name": "Asha Rao",
    "phone": "+919999900001",
    "email": "asha.rao@example.com",
    "addresses": [
        {"label": "Home", "line": "221, 17th Main", "area": "HSR Layout", "default": True},
        {"label": "Parents", "line": "4, Temple Street", "area": "Jayanagar", "default": False},
    ],
}
PROFILE_INTRO = (
    "Hi, I'm [name saved]. Reach me on [phone saved] or [email saved]. Home is [address saved],"
    " HSR Layout, and my parents are at [address saved], Jayanagar."
)

# Key facts, written by hand. `said` is the user's message; `quote` must appear in it.
# `replaces` links a superseded fact to the one that replaced it (by local id).
KEY_FACTS: list[dict[str, Any]] = [
    # superseded chains: the old value first
    {
        "id": "start_old",
        "key": "earliest_start",
        "scope": "global",
        "value": "09:00",
        "status": "superseded",
        "said": "I'm fine with slots from 9am onwards.",
        "quote": "slots from 9am onwards",
        "days_ago": 80,
    },
    {
        "id": "start",
        "key": "earliest_start",
        "scope": "global",
        "value": "10:00",
        "status": "active",
        "replaces": "start_old",
        "said": "Actually, please remember never before 10am, mornings are hectic.",
        "quote": "never before 10am",
        "days_ago": 40,
    },
    {
        "id": "plumb_budget_old",
        "key": "budget_max_inr",
        "scope": "plumber",
        "value": 1200,
        "status": "superseded",
        "said": "Keep plumbers under 1200 rupees please.",
        "quote": "plumbers under 1200",
        "days_ago": 70,
    },
    {
        "id": "plumb_budget",
        "key": "budget_max_inr",
        "scope": "plumber",
        "value": 1500,
        "status": "active",
        "replaces": "plumb_budget_old",
        "said": "Prices went up, so for plumbers up to 1500 is okay now.",
        "quote": "for plumbers up to 1500",
        "days_ago": 30,
    },
    {
        "id": "addr_old",
        "key": "default_address",
        "scope": "global",
        "value": "Parents",
        "status": "superseded",
        "said": "Use my parents' place as the default for now.",
        "quote": "parents' place as the default",
        "days_ago": 90,
    },
    {
        "id": "addr",
        "key": "default_address",
        "scope": "global",
        "value": "Home",
        "status": "active",
        "replaces": "addr_old",
        "said": "I've moved back, make Home the default address again.",
        "quote": "make Home the default address",
        "days_ago": 50,
    },
    # explicit, active
    {
        "id": "latest",
        "key": "latest_start",
        "scope": "global",
        "value": "18:00",
        "status": "active",
        "said": "Nothing after 6pm, I'm usually out in the evening.",
        "quote": "Nothing after 6pm",
        "days_ago": 45,
    },
    {
        "id": "clean_budget",
        "key": "budget_max_inr",
        "scope": "cleaning",
        "value": 2500,
        "status": "active",
        "said": "For deep cleaning I can spend up to 2500.",
        "quote": "deep cleaning I can spend up to 2500",
        "days_ago": 35,
    },
    {
        "id": "ac_budget",
        "key": "budget_max_inr",
        "scope": "ac_servicing",
        "value": 1500,
        "status": "active",
        "said": "AC servicing shouldn't cost more than 1500.",
        "quote": "AC servicing shouldn't cost more than 1500",
        "days_ago": 33,
    },
    {
        "id": "avoid_breezefix",
        "key": "avoided_provider",
        "scope": "ac_servicing",
        "value": {"provider_id": "ac_breezefix", "name": "BreezeFix"},
        "status": "active",
        "said": "Never send BreezeFix again, they came two hours late.",
        "quote": "Never send BreezeFix again",
        "days_ago": 28,
    },
    {
        "id": "dog",
        "key": "note",
        "scope": "global",
        "topic": "home_services",
        "value": "dog at home, ring the bell twice",
        "status": "active",
        "said": "Tell the technicians there's a dog at home, ring the bell twice.",
        "quote": "dog at home, ring the bell twice",
        "days_ago": 25,
    },
    {
        "id": "parking",
        "key": "note",
        "scope": "global",
        "topic": "home_services",
        "value": "visitor parking in the basement",
        "status": "active",
        "said": "They can use visitor parking in the basement.",
        "quote": "visitor parking in the basement",
        "days_ago": 20,
    },
    # forgotten: their words were scrubbed from the transcript
    {
        "id": "cats",
        "key": "note",
        "scope": "global",
        "topic": "home_services",
        "status": "forgotten",
        "said": "Oh and [forgotten], keep that in mind.",
        "days_ago": 60,
    },
    {
        "id": "wfh",
        "key": "note",
        "scope": "global",
        "topic": "work",
        "status": "forgotten",
        "said": "Also [forgotten], so weekdays are flexible.",
        "days_ago": 55,
    },
    {
        "id": "old_plumber",
        "key": "preferred_provider",
        "scope": "plumber",
        "status": "forgotten",
        "said": "[forgotten] for anything with pipes.",
        "days_ago": 75,
    },
    # inferred: pending, rejected, confirmed
    {
        "id": "sparks",
        "key": "preferred_provider",
        "scope": "electrician",
        "value": {"provider_id": "ele_sparks", "name": "Sparks Electricals"},
        "status": "pending_confirmation",
        "source": "inferred",
        "reason": "2 completed bookings with Sparks Electricals",
        "days_ago": 10,
    },
    {
        "id": "coolbreeze",
        "key": "preferred_provider",
        "scope": "ac_servicing",
        "value": {"provider_id": "ac_coolbreeze", "name": "CoolBreeze AC Care"},
        "status": "pending_confirmation",
        "source": "inferred",
        "reason": "2 completed bookings with CoolBreeze AC Care",
        "days_ago": 8,
    },
    {
        "id": "aquacare",
        "key": "preferred_provider",
        "scope": "plumber",
        "value": {"provider_id": "plm_aquacare", "name": "AquaCare Services"},
        "status": "rejected",
        "source": "inferred",
        "reason": "2 completed bookings with AquaCare Services",
        "days_ago": 65,
    },
    {
        "id": "freshnest",
        "key": "preferred_provider",
        "scope": "cleaning",
        "value": {"provider_id": "cln_freshnest", "name": "FreshNest Cleaners"},
        "status": "active",
        "source": "inferred",
        "confirmed": True,
        "reason": "3 completed bookings with FreshNest Cleaners",
        "days_ago": 15,
    },
]

# Irrelevant notes: (topic, what the user said, the quoted fact).
NOTE_TEMPLATES: list[tuple[str, str, str]] = [
    ("food", "By the way, I'm vegetarian.", "I'm vegetarian"),
    ("food", "I really prefer filter coffee over instant.", "prefer filter coffee"),
    ("food", "We order from the dosa place on weekends.", "dosa place on weekends"),
    ("food", "I'm trying to cut down on sugar.", "cut down on sugar"),
    ("food", "My son is allergic to peanuts.", "allergic to peanuts"),
    ("travel", "I travel to Pune for work every month.", "travel to Pune for work"),
    ("travel", "We're planning a Goa trip in December.", "Goa trip in December"),
    ("travel", "I prefer window seats on flights.", "prefer window seats"),
    ("travel", "My parents visit from Mysuru every summer.", "visit from Mysuru"),
    ("travel", "I take the metro to work most days.", "take the metro to work"),
    ("work", "I have standups at 9:30 every weekday.", "standups at 9:30"),
    ("work", "My office is near Koramangala.", "office is near Koramangala"),
    ("work", "Quarter-end weeks are crazy for me.", "Quarter-end weeks are crazy"),
    ("work", "I'm on calls most afternoons.", "on calls most afternoons"),
    ("work", "Fridays I finish early.", "Fridays I finish early"),
    ("family", "My daughter's school ends at 3pm.", "school ends at 3pm"),
    ("family", "My mother lives with us on weekends.", "mother lives with us on weekends"),
    ("family", "We have a newborn, so keep it quiet please.", "We have a newborn"),
    ("family", "My husband handles the car servicing.", "husband handles the car servicing"),
    ("family", "Our cook comes at 7am daily.", "cook comes at 7am"),
    ("other", "I support the local football club.", "local football club"),
    ("other", "I'm learning Kannada these days.", "learning Kannada"),
    ("other", "I read a lot of mystery novels.", "mystery novels"),
    ("other", "I volunteer at the animal shelter on Sundays.", "animal shelter on Sundays"),
    ("other", "I do yoga in the mornings.", "yoga in the mornings"),
    ("other", "My favourite colour is teal.", "favourite colour is teal"),
    ("food", "We avoid deep-fried food at home.", "avoid deep-fried food"),
    ("travel", "I renew my passport next year.", "renew my passport next year"),
    ("work", "I work hybrid, three days in office.", "three days in office"),
    ("other", "I'm saving up for a new bicycle.", "new bicycle"),
    ("family", "Grandma's birthday is in March.", "birthday is in March"),
    ("other", "I collect old coins.", "collect old coins"),
    ("food", "Mango season is my favourite time.", "Mango season"),
    ("travel", "I get carsick on long drives.", "carsick on long drives"),
    ("work", "Payday is the last working day.", "Payday is the last working day"),
    ("other", "I'm terrible with plants.", "terrible with plants"),
]

ACKS = [
    "Got it, I'll remember that.",
    "Noted, thanks for telling me.",
    "Thanks, I've saved that.",
    "Understood, I'll keep that in mind.",
]
FILLER: list[tuple[str, str]] = [
    (
        "Any update on my booking?",
        "Your booking is confirmed. I'll message you if anything changes.",
    ),
    (
        "What did I book last time?",
        "Your last booking was an electrician visit with Sparks Electricals.",
    ),
    ("Thanks, that's all for now.", "You're welcome. Message me any time."),
    ("Can you find a plumber this week?", "Sure. Which day works best for you?"),
    ("Thursday morning, please.", "I'll look for Thursday morning slots."),
    (
        "How much does AC servicing usually cost?",
        "Most providers here charge between ₹850 and ₹1,500.",
    ),
    ("Is CoolBreeze any good?", "CoolBreeze AC Care is rated 4.5 from 176 reviews."),
    ("Never mind, I'll do it later.", "No problem. I've left it for now."),
    ("Can someone fix a wardrobe hinge?", "Yes, carpenters can. Which area should I search?"),
    ("Do they bring their own tools?", "Usually yes. I can note it in the booking if needed."),
    (
        "Please check the cleaning booking for Saturday.",
        "Saturday's cleaning is confirmed for 11:00.",
    ),
    ("Who is coming for the AC?", "CoolBreeze AC Care is scheduled for your AC servicing."),
]
NOTE_ACK = "Okay, noted."


def build_seed() -> dict[str, Any]:
    rng = random.Random(SEED)
    facts = [dict(f) for f in KEY_FACTS]
    for i, (topic, said, quote) in enumerate(NOTE_TEMPLATES):
        facts.append(
            {
                "id": f"note_{i:02d}",
                "key": "note",
                "scope": "global",
                "topic": topic,
                "value": quote,
                "status": "active",
                "said": said,
                "quote": quote,
                "days_ago": rng.randint(2, 95),
            }
        )

    # Every fact the user stated gets a turn in some session (its words, then an ack).
    stated = [f for f in facts if "said" in f]
    rng.shuffle(stated)
    sessions: list[list[dict[str, str]]] = [[] for _ in range(SESSIONS)]
    sessions[0].append({"role": "user", "content": PROFILE_INTRO})
    sessions[0].append({"role": "assistant", "content": "Thanks, I've saved your details."})
    for i, fact in enumerate(stated):
        target = sessions[i % SESSIONS]
        target.append({"role": "user", "content": fact["said"]})
        ack = NOTE_ACK if fact["key"] == "note" and fact["status"] != "active" else rng.choice(ACKS)
        target.append({"role": "assistant", "content": ack})
    for session in sessions:
        while len(session) < MESSAGES_PER_SESSION:
            user, assistant = rng.choice(FILLER)
            session.append({"role": "user", "content": user})
            session.append({"role": "assistant", "content": assistant})

    memories = [
        {k: v for k, v in f.items() if k not in ("said",)}
        for f in sorted(facts, key=lambda f: (-f["days_ago"], f["id"]))
    ]
    return {
        "_comment": "Generated by errand_agent.memory.seedgen (make seed-data). Do not edit.",
        "user_id": DEMO_USER_ID,
        "profile": PROFILE,
        "memories": memories,
        "sessions": [
            {"session_id": f"sess_hist_{i + 1:02d}", "days_ago": 100 - i * 10, "messages": msgs}
            for i, msgs in enumerate(sessions)
        ],
    }


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(build_seed(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
