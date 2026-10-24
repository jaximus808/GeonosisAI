# TaskCard

A paid task in the contributor marketplace, built for one thumb on a phone.

- The consumer provides the task kind (Recovery takeover, Clean demo, Reset), the price in cents, a title that says what the person does, one sentence of instructions, the estimated duration, the robot id and the remaining slots.
- The price is set in `gx-price` (`ochre`, mono) at the top right. The task kind is a `gx-tag-money` tag.
- The title starts with a verb and describes the physical job: "Catch the cup at the bin rim", not "Episode 41".
- There is one `gx-btn-block` primary at the bottom. After a claim it becomes "Open controls". When someone else wins the claim, the card says "Taken" in a `steel` tag and the button goes away.
- Cards stack in one column with `space-4` between them and use `radius-lg`.
