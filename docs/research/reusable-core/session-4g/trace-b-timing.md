| turn start | caller said | user->bot s | STT ttfb s | model requests s | tools s | TTS ttfb s | first words |
|---|---|---|---|---|---|---|---|
| 07:23:31.726 |  | n/a | n/a | none | none | 0.00 | Hi, you've reached Sage and Stone. Robin speaking, |
| 07:23:38.083 | Hey Robin, I would like to book a haircut appointment for Fr | 2.48 | 0.39 | 1.11, 0.76 | collect 0.001; hand_over 0.002 | 0.16 | Which phone number should I use? Please say it wit |
| 07:23:47.981 | It's +34 680 840 46. 4. | 1.54 | 0.42 | 0.94 | collect 0.001 | 0.16 | Is your number +34 680 84 04 64? |
| 07:24:00.221 | That sounds about right. | 1.72 | 0.43 | 1.06, 0.87, 0.88 | lookup_customer 0.007; options 0.007; wait 0.004 | 0.21 | Thanks, I have you. |
| 07:24:12.680 | Uh, let's do 9:00 AM. | 3.74 | 0.43 | 0.91, 0.64, 0.70, 0.85 | collect 0.012; options 0.002; book_appointment 0.004; wait 0.001 | 0.15 | 9:00 AM on Friday the 2nd is free. I also have 9:3 |
| 07:24:25.340 | 9:00 AM is perfect. | 1.70 | 0.39 | 1.14 | collect 0.001 | 0.15 | Shall I book a haircut, Friday the 2nd at 9:00 AM? |
| 07:24:32.200 | Yes, please. | 1.90 | 0.39 | 1.32 | book_appointment 0.010 | 0.17 | You're all set for Friday the 2nd at 9:00 AM. Anyt |
| 07:24:38.227 | Sorry, can I— can you actually move the appointment to Satur | 0.61 | 0.44 | none | none | 0.16 | You're all set for Friday the 2nd at 9:00 AM. Anyt |
| 07:24:47.101 | If you can move the appointment to Saturday the same time. | 3.65 | 0.42 | 1.27, 0.81, 0.94 | collect 0.000; options 0.005; wait 0.001 | 0.17 | 9:00 AM on Saturday the 3rd is free. I also have 9 |
| 07:25:01.835 | Hmm, no, I asked for 9:00. | 2.31 | 0.40 | 0.91, 0.80 | collect 0.001; move_appointment 0.001 | 0.17 | Shall I move your haircut from Friday the 2nd at 9 |
| 07:25:10.859 | Yes, please. | 1.55 | 0.39 | 0.96 | move_appointment 0.008 | 0.18 | That's moved to Saturday the 3rd at 9:00 AM. Anyth |
| 07:25:18.821 | Can you actually cancel at all the appointment? | 3.58 | 0.43 | 1.27, 0.92, 0.76 | collect 0.001; options 0.004; cancel_appointment 0.001 | 0.15 | Shall I cancel your haircut Saturday the 3rd at 9: |
| 07:25:29.339 | Yes. | 2.22 | 0.29 | 1.04 | cancel_appointment 0.007 | 0.15 | That's cancelled for you. Anything else I can help |
| 07:25:35.661 | I'm good, thanks. | 1.76 | 0.40 | 1.21 | goodbye 0.000 | 0.13 | Your appointment is cancelled. Thanks for calling  |
