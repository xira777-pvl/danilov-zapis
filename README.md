# danilov-zapis

Public booking page for «В гостях у Даниловых»: a read-only availability calendar built
from Bitrix CRM deals, with the Bitrix CRM request form embedded below it.

The public calendar contains fixed slot availability and available hourly starts by
program duration. Deal details stay in Bitrix.

## Role of this repository: warm standby

The live page `danilovhouse.ru/zapis/` is built **on the hosting every 30 minutes** from the
private `danilov-morning-brief` repository. This repository's GitHub Pages build (`pages.yml`)
is the standby: the hosting publishes its `/full/` page only in calendar mode `standby`, and
only when that page was read from Bitrix after the published one (the handover rule).

## Source of truth

`brief/booking_page.py`, `brief/bitrix.py`, `brief/dates.py` are a **generated copy** of the
private repository; `brief/SOURCE_COMMIT` names the commit they come from. Do not edit them
here. After every change of the booking code there, run

    deployment/hosting/sync-zapis.sh <checkout of danilov-zapis>

in `danilov-morning-brief`; it opens a pull request here (it never merges). Merging it changes
the standby page and is the owner's decision. The hosting deploy refuses a release whose three
files differ from this repository's `main`.
