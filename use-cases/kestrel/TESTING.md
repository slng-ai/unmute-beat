# Testing Kestrel by voice

Kestrel is Sam on the Kestrel Insurance claims line. You play a driver calling in a car accident.

## How to start

```bash
cd use-cases/kestrel
uv run bot.py
```

Open http://localhost:7860 and press Start.

The seeded policies are invented:

| Policy number | Holder | Date of birth | Cars |
|---|---|---|---|
| KP482913 | Daniel | 5 June 1979 | the blue Toyota Corolla (AB12CDE) |
| KP731508 | Priya | 23 January 1992 | the white Kia Niro (LM70XYZ), the black Ford Fiesta (GH19RST) |

The claims desk lives in memory. Restart `bot.py` to reset it.

In the tables below, **code** lines are fixed and should match word for word. **Model** lines are the
model's own words, so only their meaning should match.

## Case 1: one car, one question at a time

| You say | Sam should say | Who |
|---|---|---|
| (wait) | "Thanks for calling Kestrel Insurance claims, this is Sam. Can I take your policy number, please?" | code |
| "K P four eight two nine one three." | asks for your date of birth | model |
| "Fifth of June, seventy-nine." | "Is your policy number K P, four eight two, nine one three, with a date of birth of the 5th of June, 1979?" | code |
| "Yes." | "Thank you, Daniel, I have your policy. I can take your claim now. On which day did it happen?" | code |
| "Last Monday." | "Roughly what time was it?" | code |
| "In the evening." | "Where did it happen?" | code |
| "On the A40 near Oxford." | "In your own words, what happened?" | code |
| "A van reversed into my front bumper." | "Was anyone hurt?" | code |
| "Yes, my passenger hurt her neck." | "I'm sorry to hear that. If anyone needs medical help, please call the emergency services first. Was it reported to the police? ..." | code |
| "No." | "Was another vehicle or driver involved?" | code |
| "Yes, the van." | "Can your car still be driven?" | code |
| "Yes, it drives." | "Shall I file your claim for the blue Toyota Corolla? On Monday ... in the evening, at the A40 near Oxford: ... Is that all right?" | code |
| "Yes." | "Your claim is filed. Your claim number is K C, ... A claims handler will call you within one working day. ..." | code |

Check:
1. The questions come in this order, one at a time.
2. The safety line is said once, right after you say someone was hurt.
3. The car is never asked about: the policy has only one.

## Case 2: everything in one breath, two cars, a correction

| You say | Sam should say | Who |
|---|---|---|
| "KP731508, born 23 January 1992." | the policy number and date of birth read back | code |
| "Yes." | "Thank you, Priya, I have your policy. ... On which day did it happen?" | code |
| "Yesterday afternoon in the Tesco car park in Leeds, someone scraped my door. Nobody was hurt, no police, the other driver left a note, and it still drives." | "Which car was it: the white Kia Niro or the black Ford Fiesta?" | code |
| "The Kia." | the whole claim read back for the white Kia Niro | code |
| "Actually, it was the Fiesta." | the claim read back again, now for the black Ford Fiesta | code |
| "Yes." | the claim number | code |

Check:
1. Sam asks only for what you did not already say.
2. A correction gets a fresh read-back. The claim is filed with the corrected car.

## Case 3: things that must be refused

Try each of these on a fresh call.

| You say | Sam should | Who |
|---|---|---|
| a policy number with the wrong date of birth, then "Yes" to the read-back | say it can't find that policy, and reveal no name or car | model |
| "KP12345", which is too short | ask for the number again (KP and six digits) | model |
| a day in the future, like "next Friday" | say "That day hasn't happened yet. On which day did it happen?" | code |
| "No" to the claim read-back | ask what to change, and file nothing | model |

Check:
1. Nothing about Daniel or Priya is said before a policy number and date of birth match.
2. No claim number is given unless you said yes to the summary.
