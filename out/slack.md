:red_circle: *Kestrel: 5.75% of emails bounced* (903 of 15,691 sent). Above the 5% danger line: fix the problems below before sending to new leads.
5 separate problems are causing this. Fixing them could bring it down to about 0.7%.

*1. Dead addresses in the list `apollo_oct_upload`*: 300 bounces, 1.9% of emails sent. Another 55 are spread out elsewhere
These mailboxes don't exist (5.1.1, 5.1.10, 5.4.1).
Not the inboxes or domains (spread over 12 inboxes on 3 domains). Not the email: a mailbox that doesn't exist bounces whatever it says.
→ Re-verify that list before sending to it again, and treat catch-all results as unverified.
→ Block the 322 addresses that don't exist so no campaign emails them again. Re-verify the 33 'Access denied' (5.4.1) ones first: they may be real people behind a company filter.
_Don't pause inboxes or rewrite the email: neither is the cause._

*2. `kestrelreach-hq.com` fails email authentication*: 186 bounces, 1.2% of emails sent
Receivers reject its mail because DKIM and DMARC are missing (5.7.40, 5.7.515, 4.7.40).
Not the leads or the email (spread over 3 lists and 8 variants). Your other domains, `getkestrelreach.com` and `kestrelreach.co`, pass SPF, DKIM and DMARC and get none of these bounces.
→ Add the missing DNS records.
→ Take its inboxes out of campaigns until a test email passes SPF, DKIM and DMARC.
_Don't re-verify leads: the addresses are fine._

*3. Too much volume from `jo@getkestrelreach.com`, `leo@kestrelreach.co` and `maya@kestrelreach.co`*: 163 bounces, 1.0% of emails sent. Another 6 are spread out elsewhere
Providers are slowing down their mail (4.7.0, 4.7.28, 4.7.650). They send up to 70 emails a day; the safe level is 50.
Not the leads or the email (spread over 3 lists and 8 variants). Not the whole account: the other 9 inboxes have only 6 of these bounces.
They also pile up on emails to Yahoo.
→ Lower their daily limit to 50, then raise it by at most 10-15% a week.
→ If they're still slowed down after that, check spam complaints too.
_Don't remove these leads: the addresses are fine._

*4. The email `Kestrel - Ops leaders Q4 / step 1 variant B` is being blocked as spam*: 93 bounces, 0.6% of emails sent. Another 7 are spread out elsewhere
It's blocked across different sending domains and providers, so the email itself is the problem (4.7.0, 5.7.1, 550).
Not the domains, inboxes or leads (spread over 3 sending domains, 12 inboxes and 3 lists). Not one provider either: 3 providers block it.
→ Pause this variant.
→ Its link domain trk.kestrel-links.io is on the Spamhaus blocklist: replace it in every campaign that uses it.
→ Rewrite it as plain text without links or tracking, and test it on a small batch first.
_Don't buy new domains: the problem follows the email._

*5. Mimecast is blocking mail to some recipients*: 52 bounces, 0.3% of emails sent. Another 9 are spread out elsewhere
These companies' security filter rejects the mail, while Google and Microsoft accept the same mail (550, 5.0.0, 5.7.1).
Not your domains or the email: Google and Microsoft got 12,289 of the same emails and blocked none. Not one inbox either (spread over 12).
→ Move these leads into their own campaign.
→ Send to them slowly, in plain text, from your best inboxes.
_Don't change DNS or pause inboxes: the block is on the recipient's side._

_The full report has the evidence and 17 suggested EmailBison steps. Nothing has been changed in EmailBison._

---

:large_green_circle: *Juniper: 1.12% of emails bounced* (61 of 5,432 sent). Healthy (under 2%): keep sending.
`lena@juniperhealth.io` bounces at 3x+ the client's overall rate (35 bounces): worth a look.
