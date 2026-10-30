# Bounce Triage accuracy benchmark

360 generated clients: 18 scenarios x 20 random seeds (840-22,530 sends each). Run `python bench/benchmark.py --seeds 20` to reproduce (112s).

Every client is generated with a known answer. Bounce Triage does not see it. A client counts as **correct** only if every planted cause is named (High or Medium confidence), each points at the planted list, domain, inbox, variant or gateway, and no other cause is raised at High or Medium. The two "unexpected place" scenarios plant a cause where its rule does not look first; the Gmail one also needs Gmail named as a second place.

## Per client

| Scenario | Correct | Cause found | Right place | False alarms | Notes |
|---|---:|---:|---:|---:|---|
| Healthy client (nothing hidden) | 20/20 | - | - | 0 |  |
| One bad list (dead addresses) | 20/20 | 100% | 100% | 0 | confidence High 20 |
| One domain missing DKIM and DMARC | 20/20 | 100% | 100% | 0 | confidence High 20 |
| Inboxes sending too much | 20/20 | 100% | 100% | 0 | confidence High 20; also named: yahoo (throttling) x18 |
| One email blocked as spam | 20/20 | 100% | 100% | 0 | confidence High 20 |
| Security filters blocking (Mimecast, Proofpoint) | 20/20 | 100% | 100% | 0 | confidence High 20 |
| One sending domain on a blocklist | 20/20 | 100% | 100% | 0 | confidence High 20 |
| One inbox suspended | 20/20 | 100% | 100% | 0 | confidence High 20 |
| Four problems at once | 20/20 | 100% | 100% | 0 | confidence High 80; also named: yahoo (throttling) x19, google (auth) x1 |
| Trap: bad list used in one campaign only | 20/20 | 100% | 100% | 0 | confidence High 20 |
| Trap: blocked as spam, but the link is clean | 20/20 | 100% | 100% | 0 | confidence Medium 20 |
| Trap: inboxes sending a lot, no complaints | 20/20 | - | - | 0 |  |
| Harder: bad list only a bit worse | 19/20 | 95% | 95% | 0 | confidence High 19 |
| Harder: four problems, each half as strong | 20/20 | 100% | 100% | 0 | confidence High 80; also named: yahoo (throttling) x15 |
| Too small to judge (tiny client) | 20/20 | - | - | 0 | held back as watch-list in 9/20; confidence Medium 3 |
| Cut-off bounce text (as some sequencer APIs return it) | 16/20 | 90% | 90% | 0 | confidence High 20, Medium 16; also named: google (throttling) x11 |
| Unexpected place: dead addresses from one inbox | 20/20 | 100% | 100% | 0 | confidence Medium 20; named an unexpected place: bad_data x20 |
| Unexpected place: one email blocked as spam, only at Gmail | 19/20 | 95% | 95% | 0 | confidence Medium 19; also named: google (content) x19 |

**Overall: 354/360 clients fully correct (98%).**

## Per bounce

Each planted bounce, after the filter, against the category the decoder gave it (full bounce text, all scenarios except the cut-off one).

| Planted cause | Bounces | Right category | Most common miss |
|---|---:|---:|---|
| bad_data | 38,252 | 100% | - |
| auth | 10,142 | 100% | - |
| throttling | 6,342 | 100% | - |
| content | 6,511 | 100% | unknown x16 |
| recipient_gateway | 817 | 99% | unknown x7 |
| domain_reputation | 2,806 | 100% | - |
| sender_mailbox | 1,113 | 100% | - |
| noise | 4,868 | 100% | - |

**100% of 73,226 planted bounces decoded to the right cause.**

With text cut to 200 characters and no HTML or raw copy (as some sequencer APIs return it): 75% of 10,703 bounces decoded correctly; misses: throttling as unknown x2005, bad_data as unknown x389, bad_data as noise x198, noise as unknown x121.

## What this does and doesn't prove

- It tests the logic end to end on many different clients, volumes and mixes, including lookalikes built to trick it. Each run is random, so it can't be tuned to one sample.
- The bounce texts are templates modelled on real Google, Microsoft, Yahoo, Mimecast, Proofpoint and Postfix messages, and the same code wrote the scenarios and the rules. Real bounces will be messier.
- It does not test EmailBison's real field names. Replaying a test webhook from a real workspace does that on day one.
