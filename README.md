# RouterFleet

Welcome to **RouterFleet** - the next step in centralized router backup and management. This open source project is designed to revolutionize the way we handle backups and configurations for routers and network equipment, focusing primarily on simplifying and securing network management tasks.

## Introduction

**RouterFleet** is developed with the aim of easing the management of a fleet of devices, particularly focusing on Mikrotik devices during its initial launch phase. This project is a testament to countless hours of dedication towards developing a system that not only simplifies but also secures network management tasks across various devices.

## About this fork

This repository is a fork of [eduardogsilva/routerfleet](https://github.com/eduardogsilva/routerfleet) (forked at `d9e2aa4`) that continues the project with the work described below. Everything RouterFleet does is still here, and the deployment and upgrade instructions in this README already point at this fork's files and images.

## What this fork adds

### Verified commands

Up to now a command counted as successful as soon as the router answered, which says nothing about the result: a device that reboots while it is being updated never returns an exit code at all, and a failed download does not necessarily make the install fail either.

A command variant can now define **verification commands** and the **result they have to show**. The verification runs after the payload and decides whether the task was successful:

- The payload runs **once**. A retry only verifies again, so a device that reboots in the middle of an update is never updated twice.
- Verification is repeated until the command's *verify timeout* has passed (default 300 s, one attempt every 30 s), which gives a rebooting device time to come back online.
- Every line of the expected result has to be found in the output of the verification commands. Regular expressions work, so `installed-version: 7\.1[67]\..*` is a valid expectation.
- Three placeholders are filled in from what RouterFleet knows about the router:
  - `{{ current_version }}` — the version the device runs
  - `{{ available_version }}` — the version RouterFleet read from the device's update channel
  - `{{ expected_version }}` — the version the payload itself reported as being offered (see below)
- A placeholder with no value fails **immediately** with a hint, instead of sitting in the queue until the verify timeout expires, so an expectation using `{{ current_version }}` or `{{ available_version }}` cannot pass on a router whose information was never read out. `{{ expected_version }}` needs no stored information — it comes from the payload itself.
- A task whose result was verified queues an information update, so the new version shows up in the router list right away.

The command details page shows the verification of every variant, the job details page and the device page mark the tasks whose result was verified, and the task details page shows the output the verification saw.

### Update commands that survive a channel change

The shipped update command changes the update channel (`/system/package/update/set channel=long-term`). The version RouterFleet has stored for a router was read while that router was still on its **old** channel, so comparing the installed version against it reported a perfectly successful update as failed.

The update payload therefore reports the version the router offers **after** the channel was set:

```
/system/package/update/set channel=long-term
/system/package/update/check-for-updates
:delay 5s
:put ("expected-version=" . [/system/package/update/get latest-version])
/system/package/update/download
/system/package/update/install
```

with the expectation `installed-version: {{ expected_version }}`. A payload that does not report an expected version fails right away with a hint how to add the line.

Commands that already exist are upgraded by data migrations when you upgrade. Every RouterOS command whose payload uses the package update gets the verification and a 600 s verify timeout (so a device that reboots during the install has time to come back), and a payload that installs a package also gets the `expected-version` line and the `{{ expected_version }}` expectation. A payload you wrote yourself is never replaced — the missing line is inserted into it — and a variant that defines its own verification is left alone.

### Stopping a running job or task

A job that was going wrong could only be waited out. The job list, the job details and the task details now offer a stop button (confirm by typing `abort`), which sets the tasks that have not finished yet to *aborted*.

The executor notices the abort and stops the payload before its next command — an update that was stopped during the download does not install afterwards. A command that is already running on the router cannot be interrupted from another process, so the abort takes effect before the next command or verification.

The same change fixes the run count: every execution used to increment the retry count, including the attempts of a verification, so a task with `max_retry 3` could show a run count of 8. Only a run of the payload counts as a retry now; the verification attempts are counted separately and bounded by the verify timeout. `max_retry` is the number of attempts a command makes in total, the first run included.

### Router Manager

- **Bulk edit:** select routers in the list and press **Edit** to change port, username, password, SSH key, backup profile, monitoring, enabled state and internal notes for all of them at once. A blank field leaves the current value alone. The selection is handed over in a POST request and kept in the session, so editing a few hundred routers no longer overflows the URL length limit. This also fixed the same problem for *Groups* and *Run Command*.
- **Update Information:** the device page refreshes a router's information immediately and reports the result instead of only queueing it. The router list has an **Update Information** button for the selected routers. Queued-by-hand routers are served before the regular refresh queue, and the cron task works through the whole due queue for up to 50 seconds, so a manual refresh no longer takes hours.
- **Available version and upgrades:** during the regular information update RouterFleet now finds out what the device could be upgraded to. RouterOS asks its update server, OpenWrt is compared against its release index (only releases that still ship images for the device's board are offered), airOS stays empty. The version is shown on the device page and in the router list, highlighted when an upgrade is available.
- **Router list filters:** the **Filter** button opens a line of text fields under the column titles — one per column — and each one filters its own column as you type. **Show/Hide Columns** works alongside it. Hiding the filter row clears the filters, so the list is never filtered without a visible reason. The row of filters and what stands in them are remembered in the browser, so a reload comes back to the list you were looking at; a filter is remembered by the name of its column, not by its position, so it survives showing and hiding columns and it is picked up again when that column is shown once more.
- **Search terms:** what a column filter and the search box of the list understand. Everything is matched without regard to case.
  - `text` — the cell contains it.
  - `!text` — the cell does not contain it. `!7.16` shows every router that is not on 7.16. The term is taken literally, so a `.` or a `[` in it stays a character, and a bare `!` clears the search again.
  - `^pattern` — a regular expression of your own. A column filter matches it against the cell, so `^7\.1[67]` shows the routers whose version starts with 7.16 or 7.17; the search box looks for the pattern anywhere in the row, where an anchor at the start would only ever see the first column. `!^pattern` asks for the routers that do not match it.
  - `(empty)` — only meaningful in a column filter: the rows where this column has no value. That is an empty cell as well as the `---` this list writes where there is nothing, so `(empty)` in *Groups* finds the routers that are in no group. `!(empty)` is the other way round and shows the rows that do have a value.
  - A pattern that does not compile yet — `^(` on the way to `^(a|b)` — leaves the list as it stands and marks the box until the pattern is finished.
- **Router list colours:** the firmware version is green while it is on the OS version and red while it stayed behind it. The OS version is green while it is the version the router offers, red while an update is waiting, and yellow while there is no version to compare with (a device whose update check failed, or an airOS device, which has no update check). A device that was never read out has no versions at all and stays uncoloured. Each colour carries the reason in its tooltip.
- **Address links:** what a click on the address in the router list does is a setting of your own, under **My Settings** (the gear icon in the top right of every page): nothing, **Open with SSH** (`ssh://user@address:port`), **Open with Winbox** (`winbox://address`, RouterOS devices only) or **Show a menu (SSH / Winbox)**, where moving the mouse over the address opens a small menu with both. Devices that cannot offer an action stay plain text — a monitoring device and a device without a username for SSH, anything that is not a RouterOS device for Winbox. Passwords are never part of a link.
- **CSV export:** the button beside **Select All** writes the router list into a CSV file — the rows the current search shows (the search box, the column filters and the group are all respected), across all pages, in the order they are sorted in, and with the columns that are visible. The file carries a byte order mark, so Excel opens the umlauts and accents correctly, and its name carries the date.
- **Select All** now selects what the search shows, not every router in the list. Rows that the search filtered out stay in the page (hidden), so they used to be selected as well.

### Status page

The status page shows how the fleet stands with its OS versions: how many devices are **up to date**, how many are **missing an update** and how many have **no information** at all — a device that was never read out, one whose update check failed, or a device type that has no update check (airOS). The three numbers are the colours of the router list counted up, so the overview and the list can not tell two different stories. A device that is only monitored has no version at all and stays out of all three.

### Schedules that leave a device out

A command schedule can **exclude** devices and groups. An exclusion always wins, however a device came into the schedule: a device that is selected on its own, or that belongs to a selected group, is still not executed when it stands in an excluded group or was excluded itself. That is how a device that is in both a *Location* group that should run and a *Type* group that should not is left out — exclude the *Type* group and the device is no longer run, without taking it out of any group.

A single device is excluded the same way, without touching its groups. The list of schedules says what each one runs on (`3 devices, 1 excluded`), and a schedule whose every device is excluded is refused when it is saved instead of being left behind doing nothing. Remove an exclusion and the devices are picked up again on the next run.

### Schedule defaults

A schedule needs a moment to begin at and an interval to repeat in, and both are usually the same for all of them — the hour the maintenance window opens, the week it comes back. **Schedule Defaults** (from the Fleet Commander page) sets them once:

- **Default Start Time** — the time of day. A schedule that is created starts at the *next* time this comes around, so it does not begin in the past.
- **Default Repeat Interval** — `7d`, `24h`, `30m`, or empty/`0` for a single run.

Only the form of a **new** schedule is filled from here. Schedules that already exist keep the start time and the interval they were saved with, so changing a default never moves a job that is already set up.

Editing a schedule shows its start and end time again as well: they were written into the date/time fields in a form the browser does not accept, so they came up empty and the moment had to be typed in again for every change.

The same values can be set from the command line, which is also how an installation is configured from a script or a container:

```
python manage.py schedule_defaults                          # show what is in use
python manage.py schedule_defaults --start-time 03:00 --repeat 7d
python manage.py schedule_defaults --repeat 0               # new schedules run once
python manage.py schedule_defaults --reset                  # back to 03:00 / 7d
```

In the container that is `docker compose exec routerfleet python manage.py schedule_defaults --start-time 03:00 --repeat 7d`.

### What a backup changed

Every backup now records whether it brought a different configuration than the backup before it — the field the little branch icon in the backup list has been promising without anything ever filling it. A backup that failed to read a configuration is looked past, so the comparison always reaches the last configuration that was really seen, and the very first backup of a device is no change: it is the one the later backups are measured against, not a change of anything.

The **Router Manager** list has a **Last Config Change** column (`Show/Hide Columns`, it is off by default) with the moment the configuration of that device last changed, and `---` for a device whose configuration never did. The **backup overview** marks the backups that changed something.

Clicking either one opens the change **on the page you are on** — no page navigation — as a line by line diff against the backup before it, next to a button that opens the full comparison for a change longer than the three lines of context around it. Backups you already have are filled in when you upgrade: a migration walks the history of every device and applies the same rule.

### Passwords and secrets

Passwords are no longer stored in cleartext:

- `Router.password`, the password of an import task and the passwords inside an imported CSV are encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption. Everything that connects to a router keeps reading the plaintext, so nothing else had to change.
- Existing rows are encrypted by a migration when you upgrade.
- The import details page shows bullets instead of the password, and the stored copy of the uploaded CSV keeps the password column masked.

The encryption key is generated automatically the first time the container starts and is kept in the `app_secrets` volume (`/app_secrets/encryption_key`). There is nothing to configure — but **do keep a copy of that volume**: without the key the stored passwords can no longer be decrypted, and you would have to enter them again.

### Import tool

A router group used in the CSV that did not exist aborted the whole import with `Router Group 'x' does not exist`. The import form now collects *every* missing group and asks about all of them at once: without the new **Create missing Router Groups automatically** checkbox the error lists each unknown group, with the box ticked the groups are created while the CSV is saved. They are created with `get_or_create`, so re-importing a CSV cannot create duplicates.

### Deployment

Docker images are built and published to `ghcr.io/dustinthal` by a GitHub Actions workflow on every push to `main`, and the compose files in this repository already use them. `docker compose pull` therefore picks up this fork's images, not upstream's.

## Features

- **Centralized Backup Management:** Easily manage backups for your routers and network equipment from a single interface.
- **Backup diffing:** Compare backups to identify changes and track configuration history.
- **Multiple backup profiles:** Create multiple backup profiles to manage different schedules and retention polices.
- **Mikrotik Device Compatibility:** Initial support for Mikrotik devices with plans to expand based on community feedback.
- **Continuous Updates:** Regular updates to introduce new functionalities, performance enhancements, and bug fixes.
- **Integration with wireguard:** Integration with [wireguard_webadmin](https://github.com/eduardogsilva/wireguard_webadmin) to easily manage WireGuard VPNs.
- **Open Source:** Dive into the code, contribute, and be a part of a growing community.

## Screenshots
### Backup comparison view (diff)
Easily compare backups to identify changes and track configuration history.
![Backup comparison view](screenshots/backup-diff.png)
### Multiple Backup Profiles
Create multiple backup profiles to manage different schedules and retention policies.
![Multiple Backup Profiles](screenshots/backup-profiles.png)
### Router Details
View detailed information about your routers, including the complete backup history.
![Router Details](screenshots/router-details.png)
### User Management
Manage users and their permissions to ensure secure access to RouterFleet.
![User Management](screenshots/user-manager.png)


## Deployment Instructions

These steps will guide you through deploying the RouterFleet project:

### Step 1: Prepare the Environment

Create a dedicated directory for the RouterFleet project and navigate into it. This directory will serve as your working environment for the deployment.

```bash
mkdir routerfleet && cd routerfleet
```

### Step 2: Fetch the Docker Compose File

Download the appropriate `docker-compose.yml` file directly into your working directory to ensure you're using the latest deployment configuration. Choose one of the following based on your deployment scenario:

#### With Postgres (default setup)

This is the recommended setup for production environments. Download the `docker-compose.yml` that includes the Postgres database container:

```bash
wget -O docker-compose.yml https://raw.githubusercontent.com/DustinThal/routerfleet_extended/main/docker-compose.yml
```

#### Without Postgres (sqlite or remote database)

If you prefer to use SQLite or a remote database, download the `docker-compose-no-postgres.yml` file:

```bash
wget -O docker-compose.yml https://raw.githubusercontent.com/DustinThal/routerfleet_extended/main/docker-compose-no-postgres.yml
```

### Step 3: Create the `.env` File

Generate a `.env` file in the same directory as your `docker-compose.yml` with the necessary environment variables:

```env
# Configure SERVER_ADDRESS to match the address of the server. If you don't have a DNS name, you can use the IP address.
# A missconfigured SERVER_ADDRESS will cause the app to have CSRF errors.
SERVER_ADDRESS=my_server_address
DEBUG_MODE=False
# Choose a timezone from https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
TIMEZONE=America/Sao_Paulo

# Available options are 'sqlite', 'postgres'
DATABASE_ENGINE=postgres
# If you want to use sqlite or postgres outside of docker, you should use docker-compose-no-postgres.yml
# and provide POSTGRES_HOST, POSTGRES_PORT below.
#POSTGRES_HOST=
#POSTGRES_PORT=
POSTGRES_DB=routerfleet
POSTGRES_USER=routerfleet
POSTGRES_PASSWORD=your_database_password
```

Adjust the variables according to your setup.

### Step 4: Run Docker Compose

If you are upgrading from a previous version, you should consider running ```docker compose pull``` to ensure you are using the latest images.

Start the RouterFleet services using Docker Compose:

```bash
docker compose up -d
```

### Step 5: Update SSL Certificates (Optional)

If you prefer to use your own SSL certificates instead of the auto-generated self-signed certificate:

- Access the `certificates` volume.
- Replace `nginx.pem` and `nginx.key` with your certificate files.

### Step 6: Access the Web Interface

Visit `https://yourserver.example.com` in your web browser to access the RouterFleet web interface. Remember, if you're using the self-signed certificate, you'll need to accept the certificate exception in your browser.

Following these steps will set up RouterFleet on your server, ensuring you're utilizing the latest configurations for optimal performance and security.

## Upgrade Instructions for RouterFleet

To maintain security, performance, and access to new features in RouterFleet, it's important to follow these steps when upgrading:


### Step 1: Prepare the Environment
  
   Begin by navigating to your routerfleet directory:
   ```bash
   cd path/to/routerfleet
   ```

### Step 2: Backup Database

   Before starting the upgrade, it's crucial to back up your database. This step ensures you can revert to the previous state if the upgrade encounters problems. For the database, we recommend manually running a `pg_dump` command to create a backup.
```bash
docker exec -e PGPASSWORD=your_password routerfleet-postgres pg_dump -U routerfleet -d routerfleet > /root/routerfleet-$(date +%Y-%m-%d-%H%M%S).sql
```

   Also back up the `app_secrets` volume, which holds the key that the router passwords in your database are encrypted with. A database backup without that key cannot restore the passwords. Compose prefixes the volume with the project name — the name of your `routerfleet` directory — so substitute yours (listed by `docker volume ls`) for `<project>`:
```bash
docker run --rm -v <project>_app_secrets:/secrets -v "$PWD":/backup alpine tar czf /backup/app_secrets-$(date +%Y-%m-%d-%H%M%S).tar.gz -C /secrets .
```

### Step 3: Shutdown Services

   Prevent data loss by stopping all RouterFleet services gracefully:
   ```bash
   docker compose down
   ```

### Step 4: Update Docker Compose File

   Download the latest `docker-compose.yml` file from the repository to ensure you're using the most recent version:
   ```bash
   wget -O docker-compose.yml https://raw.githubusercontent.com/DustinThal/routerfleet_extended/main/docker-compose.yml
   ```
   Alternatively, if you're using SQLite or a remote database, download the `docker-compose-no-postgres.yml` file:
   ```bash
   # (alternative) No postgres container 
   wget -O docker-compose.yml https://raw.githubusercontent.com/DustinThal/routerfleet_extended/main/docker-compose-no-postgres.yml
   ```

### Step 5: Update image files

   Pull the latest images to ensure you're using the most recent versions:
   ```bash
   docker compose pull
   ```

### Step 6: Run Docker Compose

   Start the RouterFleet services using Docker Compose:

   ```bash
   docker compose up -d
   ```

### Post-Upgrade Checks

   - **Verify Operation:** After the services start, access the web interface to ensure routerfleet functions as expected. Examine the application logs for potential issues.
   - **Support and Troubleshooting:** For any complications or need for further information, consult the project's [Discussions](https://github.com/DustinThal/routerfleet_extended/discussions) page or relevant documentation.

Following these instructions will help ensure a smooth upgrade process for your RouterFleet installation, keeping it secure and efficient.


## Contributing

As an open source project, RouterFleet thrives on community support. Whether you're a developer, a network engineer, or just someone interested in network management, there are many ways you can contribute:

- **Code Contributions:** Submit pull requests with bug fixes, new features, and improvements.
- **Feedback:** Share your experiences, suggest improvements, and help shape the future of RouterFleet.
- **Documentation:** Help improve the documentation to make RouterFleet more accessible to everyone.
- **Testing:** Report bugs, test new features, and help ensure RouterFleet is stable and reliable.


## Support and Community

Join our community to get support, share ideas, and collaborate:

- [GitHub Issues](https://github.com/DustinThal/routerfleet_extended/issues) for reporting bugs and feature requests.
- [Discussions](https://github.com/DustinThal/routerfleet_extended/discussions) for sharing ideas and getting help from the community.

Your support and involvement are crucial in shaping the future of RouterFleet. Let's make network management easier and more secure together!

## License

RouterFleet is released under the [MIT License](LICENSE). Feel free to explore, modify, and distribute the software as per the license agreement.

---

We look forward to your contributions and are excited to see how RouterFleet evolves with your help and feedback. Let's build a robust community around efficient and secure network management. Thank you for your support!
