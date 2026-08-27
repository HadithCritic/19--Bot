# Privacy Policy — 19 Bot

**Last updated:** 27 August 2026  
**Applies to:** the 19 Bot Discord application ("the Bot")  
**Contact:** jonathan [at] wikisubmission [dot] org

19 Bot is a moderation and utility bot operated for a single Discord community,
The Submission Server. This policy describes exactly what the Bot stores, why,
how long it is kept, and how to have it deleted.

---

## 1. Who is responsible

The Bot is operated by its developer team, reachable at
jonathan [at] wikisubmission [dot] org. There is no company, no third-party processor, and
no analytics provider involved.

## 2. What the Bot stores

The Bot stores data in a single SQLite database on the operator's own machine.
It does not use a hosted database, a cloud service, or any third-party API for
storage.

| Data | Why it is stored | Where |
|---|---|---|
| Discord user ID, username, display name | To identify who a moderation record applies to | `suspicious_accounts` |
| Account creation date, server join date | To detect throwaway accounts created shortly before joining | `suspicious_accounts` |
| Timeout reason, the moderator who applied it, the date | To keep an auto-renewing timeout in force and let staff review it | `perpetual_timeouts` |
| Discord user ID and a reason | To re-apply a ban if a removed user rejoins | `after_ban_users` |
| Discord user ID and an expiry timestamp | To remove the New Member role after 24 hours | `new_member_roles` |
| Event title, description, voice channel, organiser ID, image URL | To store live events that staff create | `custom_live_events` |
| Bot application IDs | The list of bots allowed to join | `whitelist` |
| Last-seen article URL per RSS feed | So the same article is not announced twice | `blog_state` |

### Data the Bot reads but does not store

The Bot reads the text of messages in channels it can see, in order to block
links posted by members still inside the new-member period. **Message content
read for this purpose is never written to disk.** It is examined in memory and
discarded. Deleted messages are not logged or retained.

### Historical voice-channel data

An earlier version of the Bot recorded voice channel joins. That feature has
been removed and nothing writes to the table any more. The historical records
remain in the operator's local database as an archive and are not used by any
command. They will be deleted on request, or when the operator drops the table.

## 3. Channel archiving

The Bot provides an `/archive` command that saves the full history of a
specified channel to the operator's local disk, including message text,
authors, timestamps, and attached files.

**This is important, so it is stated plainly:**

- The command can **only** be run by the single account that owns the
  application. It cannot be used by server administrators, moderators, or any
  other member, and it is hidden from everyone else in the Discord client.
- It only reads channels the Bot has already been granted access to by the
  server's own permission settings. It cannot read private channels the Bot
  cannot see.
- Archives are written to local disk on the operator's machine. They are never
  uploaded, transmitted to a third party, published, sold, or shared.
- Every archive run is logged, and the fact that a channel was archived is
  recorded in the `archive_runs` table.
- Archives are retained only as long as the operator needs them and are deleted
  on request.

If you are a member of a server where this Bot is installed and you object to
your messages being included in such an archive, contact the operator at the
address above and your messages will be removed from any existing archive.

## 4. What the Bot does NOT do

- It does not sell, rent, licence, or transfer your data to anyone.
- It does not share data with any third party, advertiser, or analytics service.
- It does not use your data to train machine learning models.
- It does not track you across servers, or collect data from servers other than
  the one it is configured for.
- It does not store message content for the link-blocking feature.
- It does not read or store direct messages between users.
- It does not collect payment information, email addresses, IP addresses, or
  any data outside Discord.

## 5. Retention

| Data | Retention |
|---|---|
| New Member role records | Deleted automatically once the role expires (24 hours) |
| After-ban entries | Deleted automatically once the ban is applied |
| Perpetual timeout records | Kept until a moderator removes the timeout |
| Suspicious account flags | Kept while the moderation record remains relevant; deleted on request |
| Live events | Kept until deactivated by staff; deleted on request |
| RSS feed state | Overwritten continuously; contains no personal data |
| Channel archives | Kept only as long as needed by the operator; deleted on request |
| Logs | Rotated automatically, keeping at most 5 files of 5 MB each |

## 6. Your rights

You may request, at any time and free of charge:

- **Access** — a copy of the data the Bot holds about you.
- **Correction** — correction of inaccurate data.
- **Deletion** — erasure of your data, including removal from any channel
  archive.
- **Objection** — that the Bot stop processing your data, which in practice
  means removing your records and, if you wish, the Bot leaving your server.

To exercise any of these, email **jonathan [at] wikisubmission [dot] org** from an address
you control, or contact the operator directly on Discord, including your Discord
user ID. Requests are actioned within 30 days.

Note that moderation records exist to keep a community safe. A deletion request
that would erase an active ban or timeout record may be declined for as long as
that enforcement action is in force. You will be told if that is the case and
why. Everything else will be deleted.

## 7. Security

- The database and any archives are stored on the operator's own machine, not on
  a public server.
- Credentials are held in environment variables, never in source control.
- Access is limited to the application's developer team, all of whom are
  required by Discord to use two-factor authentication.

## 8. Children

Discord requires users to be at least 13, or older where local law requires. The
Bot is not directed at children and does not knowingly collect data from anyone
below Discord's minimum age. If such data has been collected, contact the
operator and it will be deleted.

## 9. Changes

Material changes to this policy will be announced in the server the Bot serves,
and the "Last updated" date above will change. The current version is always
available at the URL configured in the Discord Developer Portal.

## 10. Discord

The Bot operates inside Discord and is subject to Discord's own
[Privacy Policy](https://discord.com/privacy) and
[Terms of Service](https://discord.com/terms). Data you provide to Discord
itself is governed by those documents, not this one.
