# Testing Nosh by voice

Nosh helps you find a restaurant or a bar. You play the person it calls back.

## How to start

Nosh calls the real place tools on Render, so it needs the bearer token. Add it to the root `.env` once:

```
TOOLS_API_KEY=<the token>
```

Then:

```bash
cd use-cases/nosh
uv run bot.py
```

Open http://localhost:7860 and press Start. The first search can take a few seconds while Render wakes up.

The places come from Google, so names, ratings and hours change. In the tables below, **code** lines have
fixed wording around the place data. **Model** lines are the model's own words, so only their meaning
should match.

## Case 1: an area and a craving

| You say | Nosh should say | Who |
|---|---|---|
| (wait) | "Hey, it's Nosh! You wanted a hand finding somewhere good. Whereabouts are you looking? An address or an area is fine." | code |
| "Tapas in the Gothic Quarter, Barcelona." | "So that's Barri Gòtic, Barcelona, Spain, right?" | code |
| "Yes." | up to three places, each with its kind, its rating and how many reviews, then "Do any of those sound good?" | code |
| "The second one." | that place's address, rating, price and today's hours, then "Want to hear about another one, or is this the one?" | code |
| "And the first?" | the first place's details | code |
| "That's the one, thanks. Bye." | "Enjoy your meal! Bye for now." and the call ends | code |

Check:
1. Every rating, price and opening time matches Google Maps for that place.
2. Nosh never says a price or hours that the details did not give.

## Case 2: a street address, no craving

| You say | Nosh should say | Who |
|---|---|---|
| (wait) | the greeting and "Whereabouts are you looking?" | code |
| "I'm at Passeig de Sant Joan 55, Barcelona." | "So that's ..., right?" | code |
| "Yes." | places near that address, with no question about food first | code |
| "Actually, I'd rather have sushi." | sushi places near the same spot | code |

Check:
1. An exact address searches nearby straight away. An area with no craving asks "What are you in the mood
   for? A cuisine, a bar, or no preference?" first. Try both.
2. A new craving gives a new list. The spot is not asked again.

## Case 3: a place Google can't find, then a change of area

| You say | Nosh should say | Who |
|---|---|---|
| "Somewhere in Nowheresville." | "Hmm, I couldn't place that. Could you give me a nearby cross street, a neighbourhood or the city?" | code |
| "Okay, Gràcia in Barcelona, a cocktail bar." | "So that's Gràcia, Barcelona, Spain, right?" | code |
| "Yes." | cocktail bars in Gràcia | code |
| "Wait, what about Born instead?" | asks you to confirm the new area, then a new list | code |

Check:
1. After a change of area, Nosh never gives details of a place from the old list.
2. After a place's details, ask "What's their phone number?". Nosh should read it out from the details.
   Then ask "Can you text it to me?". Nosh should say it can't send messages. Texting was dropped on purpose.

Hours are "today" in Madrid time (`current_datetime` in `nosh/agent.yaml`). A place in another time zone
can show the wrong day's hours.
