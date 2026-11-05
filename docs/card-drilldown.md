# Card drill-down: exploring informative cards in place

Informative view cards are not dead ends. A row opens where it is, in the card column: no
navigation, no question, no selection, nothing sent to the server.

- A row with its own https `url` opens it with Link, as before.
- A row without one is a button: clicking it shows the row in full (its line limits lift) plus any
  supplied field the collapsed row doesn't draw. A chevron in the card's leading padding marks it
  (it draws over the padding, so collapsed cards keep their layout). Clicking again closes it.
  Opening and closing animate, and the animation is off when Reduce Motion is on.
- Only supplied, nonblank fields appear (`CardDetail`). Nothing is fetched or invented.

| Card | Opens to |
| --- | --- |
| calendar_day / agenda event | full title, time and place (events with a url open it) |
| places row | full name and details, plus `address`, `hours_today`, `phone` if supplied; the arrow still opens the place's page or Maps |
| inbox / message | full subject and preview; a supplied `body` is a second disclosure inside |
| news story without a url | full headline |
| package with more than six steps | every supplied step, each in full |
| comparison row | clicking the row label lifts the 3-line limit on that row's values; the winner's seal reads "Winner" |
| media overview, entity summary | full text (collapsed: 4 lines) |
| chart | main's hover/drag read-out, now built from `chartPoints`, so a null value no longer moves later values onto the wrong label |

Cards with nothing hidden (list, steps, stats, nutrition, contact, place) have no disclosure:
expanding them would only repeat text the card already shows.

Accessibility: a card is an accessibility container (`.contain`), so its row buttons and links
stay usable. Rows that open have the "Shows details" hint and an Expanded/Collapsed value, and
what they open is read from the row itself; the card's own label stays its summary. A changed
card starts with every row closed.

## Contract

Nothing changes on the plugin or the server. Today's catalog (`plugin/speakeasy/views.py`) has no
`body` on inbox or message rows and no address or hours on places rows, so those disclosures only
show up if a later contract change adds the fields.

## Code

- `mac/Sources/SpeakeasyCore/CardDetail.swift`: `CardDetail` (supplied fields, https-only
  destination) and `ViewCard.chartPoints`.
- `mac/Sources/SpeakeasyClient/Cards/CardDisclosure.swift`: `CardDisclosure` and `CardDetailRow`.
- Tests: `mac/Tests/SpeakeasyCoreTests/CardDetailTests.swift`.
