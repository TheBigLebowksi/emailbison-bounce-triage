# Bounce Triage report

Mode: `replay`. DNS/DBL: **mock** (values from samples/*_mock.json). Send counts: **mocked** (samples/sends.csv). LLM: **off (template summary)**. Actions: **dry run**.

| Client | Health | Sends | Real bounces | Bounce rate | Hard | Soft | Causes found |
|---|---|---:|---:|---:|---:|---:|---|
| Kestrel | RED | 15,691 | 903 | 5.75% | 2.26% | 1.60% | bad_data (High), auth (High), throttling (High), content (High), recipient_gateway (High) |
| Juniper | GREEN | 5,432 | 61 | 1.12% | 0.53% | 0.42% | none - healthy |

## Kestrel

**Health: RED** - 5.75% (green below 1.5%, red above 5.0%; youngest sending domain 19 days). Fix the causes below before sending to new leads.

Kestrel: 903 real bounces on 15,691 sends (5.75%). 5 separate causes, not one: Bad data (High, 2.26 pts); Authentication (High, 1.19 pts); Volume throttling (High, 1.08 pts); Copy fingerprint (High, 0.64 pts); Recipient gateway (High, 0.39 pts). Fixing the High/Medium ones could bring the rate down to about 0.7%. Of the 17 suggested EmailBison steps (all dry run), 11 are safe to run on their own; 6 need a person.

Bounce class (share of sends): hard 2.26%, soft 1.60%, block 1.84%.

Input: 1036 webhook events -> 41 duplicate retries, 12 auto-replies, 74 delay notices, 6 non-bounce events dropped -> **903 real bounces**.

| # | Cause | Confidence | Bounces | Pts of rate | Hard pts | Soft pts | Where |
|---|---|---|---:|---:|---:|---:|---|
| 1 | Bad data | High | 355 | 2.26 | 2.26 | 0.00 | `apollo_oct_upload` |
| 2 | Authentication | High | 186 | 1.19 | 0.00 | 0.20 | `kestrelreach-hq.com` |
| 3 | Volume throttling | High | 169 | 1.08 | 0.00 | 1.08 | `jo@getkestrelreach.com`, `leo@kestrelreach.co`, `maya@kestrelreach.co` |
| 4 | Copy fingerprint | High | 100 | 0.64 | 0.00 | 0.24 | `Kestrel - Ops leaders Q4 / step 1 variant B` |
| 5 | Recipient gateway | High | 61 | 0.39 | 0.00 | 0.00 | `mimecast` |
| 6 | Noise | Low | 18 | 0.11 | 0.00 | 0.09 | spread |

### 1. Bad data: invalid addresses from one list source - High confidence

Signals: code yes, concentration yes, check yes.

**Evidence**
- 355 bounces = 2.26% of 15,691 sends (2.26 pts of the bounce rate).
- Codes: 5.1.1 google x140, 5.1.10 microsoft x86, 5.4.1 microsoft x33, 5.1.1 proofpoint x23; 81% matched a specific code-book entry.
- Concentration: list source `apollo_oct_upload` = 24.7% of sends but 84.5% of these bounces (lift 3.4, rate 7.74% on 3,877 sends).
- Spread: across 12 inboxes (needs >= 3; largest holds 17%).
- Verifier: 74.0% of these bounced leads were not 'valid' at upload (catch_all 129, unknown 70, valid 70). The 31 ambiguous 5.4.1 'Access denied' bounces on this list are not counted as verifier misses.
- Note: these also over-index on `Kestrel - Finance VPs Q4 / step 1 variant A` (lift 3.0) because that list was loaded into that campaign. That is the list, not the copy - do not pause the variant for it.
- Role addresses (info@, sales@, support@...): 0 of 355 bounced leads (0.0%); keep them under 5% of a batch.
- Consumer domains: 18.0% of these bounced leads are on yahoo.com, outlook.com, gmail.com... On a B2B list, drop them or verify them separately.
- Verification age: 287 of 342 were verified more than 14 days before the send. Re-verify at send time.

**Fix**
- Re-verify the hot list source with a second verifier and treat catch-all as unverified.
- Suppress every bounced address (blocklist) so no other campaign retries it.
- Pause uploads from that source until it passes a 200-lead sample test under 2% bounce.
- Smoke-test 100 messages and require < 1% bounce, then resume at 40-50% volume for a week.

**Do NOT**
- Do not pause inboxes or domains: they are spread evenly and are not the cause.
- Do not rewrite copy: 5.1.x means the address does not exist, whatever the email says.

**Actions (dry run)**
- [auto] `POST /api/blacklisted-emails` x322 - Never retry an address that does not exist.
- [manual] 33 addresses bounced with an ambiguous code (5.4.1 'Access denied'): they may be real people behind a company filter. Re-verify them before blocking.
- [auto] `POST /api/tags` `{"name": "bounce:bad-data"}` - Make the cause filterable in EmailBison.
- [auto] `POST /api/tags/attach-to-leads` `{"tag_ids": ["<id of bounce:bad-data>"], "lead_ids": [10040, 10075, 10081, 10095, 10097, 10100, 10105, 1011...` - Tag bounced leads `bounce:bad-data`.
- [manual] Stop uploads from list source `apollo_oct_upload` and re-verify its un-contacted leads (no list-source object in the API; done in the enrichment tool).

### 2. Authentication: sending domain fails SPF/DKIM/DMARC - High confidence

Signals: code yes, concentration yes, check yes.

**Evidence**
- 186 bounces = 1.19% of 15,691 sends (1.19 pts of the bounce rate).
- Codes: 5.7.40 google x73, 5.7.515 microsoft x68, 4.7.40 google x31, 5.7.9 yahoo x14; 92% matched a specific code-book entry.
- Concentration: sending domain `kestrelreach-hq.com` = 21.5% of sends but 100.0% of these bounces (lift 4.7, rate 5.52% on 3,372 sends).
- DNS: kestrelreach-hq.com - DKIM missing on common selectors, DMARC missing (mock).
- Control: getkestrelreach.com, kestrelreach.co pass all three and show no auth bounces.

**Fix**
- Publish the missing records (SPF include for the mailbox provider, DKIM key, DMARC p=none with rua) and confirm alignment.
- Pause the domain's inboxes in all campaigns until a test message shows spf=pass dkim=pass dmarc=pass.
- Smoke-test 100 messages and require < 1% bounce, then resume at 40-50% volume for a week.

**Do NOT**
- Do not re-verify leads: the addresses are fine, the receiver rejects the sender.
- Do not move leads to other inboxes on the same domain: they fail the same check.

**Actions (dry run)**
- [needs approval] `DELETE /api/campaigns/201/remove-sender-emails` `{"sender_email_ids": [409, 410, 411, 412]}` - Pull dan@kestrelreach-hq.com, eve@kestrelreach-hq.com, kai@kestrelreach-hq.com, zoe@kestrelreach-hq.com out of campaign 201 (EmailBison has no domain-level pause).
- [needs approval] `DELETE /api/campaigns/202/remove-sender-emails` `{"sender_email_ids": [409, 410, 411, 412]}` - Pull dan@kestrelreach-hq.com, eve@kestrelreach-hq.com, kai@kestrelreach-hq.com, zoe@kestrelreach-hq.com out of campaign 202 (EmailBison has no domain-level pause).
- [auto] `POST /api/tags` `{"name": "bounce:auth"}` - Make the cause filterable in EmailBison.
- [auto] `POST /api/tags/attach-to-sender-emails` `{"tag_ids": ["<id of bounce:auth>"], "sender_email_ids": [409, 410, 411, 412]}` - Tag affected inboxes `bounce:auth`.

### 3. Volume throttling: inboxes pushed above safe daily volume - High confidence

Signals: code yes, concentration yes, check yes.

**Evidence**
- 169 bounces = 1.08% of 15,691 sends (1.08 pts of the bounce rate).
- Codes: 4.7.0 [TSS04] yahoo x76, 4.7.28 google x70, 4.7.650 microsoft x23; 100% matched a specific code-book entry.
- Concentration: inbox `jo@getkestrelreach.com` = 13.4% of sends but 40.2% of these bounces (lift 3.0, rate 3.24% on 2,101 sends).
- Concentration: inbox `leo@kestrelreach.co` = 13.4% of sends but 30.8% of these bounces (lift 2.3, rate 2.48% on 2,101 sends).
- Concentration: inbox `maya@kestrelreach.co` = 13.4% of sends but 25.4% of these bounces (lift 1.9, rate 2.05% on 2,101 sends).
- Also piles up on recipient provider `yahoo` (lift 7.1 within the bounces above, too many to be chance).
- Volume: jo@getkestrelreach.com sends 70/day (was 42/day, safe cap 50, EmailBison limit 75).
- Volume: leo@kestrelreach.co sends 70/day (was 42/day, safe cap 50, EmailBison limit 75).
- Volume: maya@kestrelreach.co sends 70/day (was 42/day, safe cap 50, EmailBison limit 75).
- Control: the other 9 inboxes average 35/day.

**Fix**
- Lower the daily limit on the hot inboxes back to their cap.
- Ramp volume by at most 10-15% per week per inbox.
- Add inboxes instead of pushing existing ones when the plan needs more sends.

**Do NOT**
- Do not suppress these leads: 4xx deferrals say nothing about the address.
- Do not pause the campaign: the other inboxes are fine.

**Actions (dry run)**
- [needs approval] `PATCH /api/sender-emails/daily-limits/bulk` `{"sender_email_ids": [406, 403, 402], "daily_limit": 50}` - Bring hot inboxes back under the safe cap.
- [auto] `POST /api/tags` `{"name": "bounce:throttling"}` - Make the cause filterable in EmailBison.
- [auto] `POST /api/tags/attach-to-sender-emails` `{"tag_ids": ["<id of bounce:throttling>"], "sender_email_ids": [402, 403, 406]}` - Tag affected inboxes `bounce:throttling`.

### 4. Copy fingerprint: one variant blocked across domains and providers - High confidence

Signals: code yes, concentration yes, check yes.

**Evidence**
- 100 bounces = 0.64% of 15,691 sends (0.64 pts of the bounce rate).
- Codes: 4.7.0 google x22, 5.7.1 google x21, 550 mimecast x16, 4.7.28 google x15; 100% matched a specific code-book entry.
- Concentration: step/variant `Kestrel - Ops leaders Q4 / step 1 variant B` = 12.5% of sends but 93.0% of these bounces (lift 7.5, rate 4.76% on 1,955 sends).
- Spread: across 3 sending domains (needs >= 2; largest holds 43%).
- Spread: across 3 recipient providers (needs >= 2; largest holds 55%).
- Spamhaus DBL: link domain trk.kestrel-links.io listed as 'spam domain' (127.0.1.2, mock).
- Excluded: 45 address bounces (5.1.x) on the same variant(s) are bad data, not copy, and are counted under that cause.

**Fix**
- Pause the hot variant.
- Replace the link domain trk.kestrel-links.io in every campaign that uses it: it is on the Spamhaus DBL. Request delisting once it is out of use.
- Send step 1 as plain text with no links and no open/click tracking.
- Rewrite with word-level spintax so each send is near-unique; re-test at 5% of volume before resuming.

**Do NOT**
- Do not re-verify leads: these are content blocks, not invalid addresses.
- Do not buy new domains: the copy follows you to any domain.

**Actions (dry run)**
- [needs approval] `PATCH /api/campaigns/v1.1/sequence-steps/8002/activate-or-deactivate` `{"active": false}` - Deactivate `Kestrel - Ops leaders Q4 / step 1 variant B`.
- [auto] `POST /api/tags` `{"name": "bounce:content"}` - Make the cause filterable in EmailBison.
- [auto] `POST /api/tags/attach-to-campaigns` `{"tag_ids": ["<id of bounce:content>"], "campaign_ids": ["201"]}` - Tag campaign(s) `bounce:content`.

### 5. Recipient gateway: Mimecast/Proofpoint/Barracuda policy blocks - High confidence

Signals: code yes, concentration yes, check yes.

**Evidence**
- 61 bounces = 0.39% of 15,691 sends (0.39 pts of the bounce rate).
- Codes: 550 mimecast x32, 5.0.0 mimecast x20, 5.7.1 proofpoint x9; 100% matched a specific code-book entry.
- Concentration: recipient provider `mimecast` = 6.8% of sends but 85.2% of these bounces (lift 12.5, rate 4.86% on 1,071 sends).
- Control: same block category on Google/Microsoft recipients = 0 of 12,289 sends (0.00%).
- Sender side ruled out: 84% of these blocks came from domains that pass SPF/DKIM/DMARC and are not DBL-listed.

**Fix**
- Split leads behind that gateway into their own campaign.
- Send to that segment at low volume, plain text, from your best-reputation inboxes.
- Tighten targeting: gateway-protected companies are often enterprise accounts that need a different motion.

**Do NOT**
- Do not touch DNS: Google and Microsoft accept the same mail.
- Do not pause inboxes: the block is on the recipient side.

**Actions (dry run)**
- [auto] `POST /api/tags` `{"name": "bounce:recipient-gateway"}` - Make the cause filterable in EmailBison.
- [auto] `POST /api/tags/attach-to-leads` `{"tag_ids": ["<id of bounce:recipient-gateway>"], "lead_ids": [10089, 10186, 10391, 10623, 10652, 10730, 10...` - Tag bounced leads `bounce:recipient-gateway`.

### 6. Noise: mailbox full, loops, expired 4xx - Low confidence

Signals: code yes, concentration no.

**Evidence**
- 18 bounces = 0.11% of 15,691 sends (0.11 pts of the bounce rate).
- Codes: 4.2.2 google x10, 4.4.7 microsoft x4, 5.2.2 microsoft x4; 100% matched a specific code-book entry.
- Concentration: noise clusters on list source `clay_enriched` - worth a look.

**Fix**
- No action. These clear on their own; EmailBison's normal bounce handling is enough.

**Do NOT**
- Do not pause anything for these.

**Actions (dry run)**
- none

### Watch list (below minimum sample, no verdict yet)

- Domain reputation: 4 bounces (5.7.708 microsoft x4) on dan@kestrelreach-hq.com, eve@kestrelreach-hq.com, kai@kestrelreach-hq.com
- Sender mailbox: 6 bounces (5.4.5 google x6) on ivy@getkestrelreach.com

### Unknown queue (4 bounces kept with raw text)

Add a code-book entry for any pattern that repeats. LLM suggestions are shown, never used in the verdict.

- `550 Message rejected by recipient MTA (R7)`
- `553 sorry, that domain isn't in my list of allowed rcpthosts (#5.7.1)`
- `554 Transaction failed`
- `550 5.7.0 Rejected - see https://help.example-filter.net/r/77`

### Sending domain checks

| Domain | SPF | DKIM | DMARC | DBL | Source |
|---|---|---|---|---|---|
| getkestrelreach.com | yes | google | quarantine | clean | mock / mock |
| kestrelreach-hq.com | yes | MISSING | MISSING | clean | mock / mock |
| kestrelreach.co | yes | google | none | clean | mock / mock |

## Juniper

**Health: GREEN** - 1.12% (green below 2.0%, red above 5.0%; youngest sending domain 120 days). Keep sending.

Juniper: 61 real bounces on 5,432 sends (1.12%). No cause passes the evidence bar, but lena@juniperhealth.io bounces at 3x+ the workspace rate: check that inbox.

Bounce class (share of sends): hard 0.53%, soft 0.42%, block 0.15%.

Input: 64 webhook events -> 1 duplicate retries, 0 auto-replies, 2 delay notices, 0 non-bounce events dropped -> **61 real bounces**.

| # | Cause | Confidence | Bounces | Pts of rate | Hard pts | Soft pts | Where |
|---|---|---|---:|---:|---:|---:|---|
| 1 | Noise | Low | 13 | 0.24 | 0.00 | 0.22 | spread |

### 1. Noise: mailbox full, loops, expired 4xx - Low confidence

Signals: code yes, concentration no.

**Evidence**
- 13 bounces = 0.24% of 5,432 sends (0.24 pts of the bounce rate).
- Codes: 4.7.1 other x5, 4.4.7 microsoft x4, 4.2.2 google x3, 5.2.2 microsoft x1; 100% matched a specific code-book entry.
- Concentration: noise clusters on sending domain `juniperhealth.io` - worth a look.

**Fix**
- No action. These clear on their own; EmailBison's normal bounce handling is enough.

**Do NOT**
- Do not pause anything for these.

**Actions (dry run)**
- none

### Watch list (below minimum sample, no verdict yet)

- Sender mailbox: 8 bounces (5.4.5 google x8) on lena@juniperhealth.io
- Inbox outlier: 35 bounces on lena@juniperhealth.io: 3.76% of 932 sends, 3.3x the workspace rate (1.12%)

### Baseline (spread out and below the materiality bar)

- Bad data: 29 bounces (0.53%)
- Volume throttling: 11 bounces (0.20%)

### Sending domain checks

| Domain | SPF | DKIM | DMARC | DBL | Source |
|---|---|---|---|---|---|
| juniper-mail.net | yes | default | none | clean | mock / mock |
| juniperhealth.io | yes | google | none | clean | mock / mock |

