---
url: https://docs.example.com/widgets/handbook
title: Widget Handbook - Example Docs
fetched: 2026-10-03T12:00:00+00:00
hash: 5d41402abc4b
---
Title: Widget Handbook - Example Docs

URL Source: https://docs.example.com/widgets/handbook

Markdown Content:
[Skip to main content](https://docs.example.com/widgets/handbook#main)

# Widget Handbook

[[p=0.12]] This handbook describes how to install, configure and retire widgets in the example fleet. A widget is a small service that answers requests on one port and keeps its state in a local directory. Every widget belongs to one team, and that team is paged when the widget stops answering. The handbook is written for operators who already know the fleet tools.

The sections below follow the life of a widget from its first install to its removal. Each section lists the commands that change state and the files those commands touch, so an operator can check the result by reading the files rather than trusting the command output.

## Installing widgets[](https://docs.example.com/widgets/handbook#installing-widgets)

[[p=0.91]] A widget is installed with the `widgetctl install` command. The command takes a name and a size, writes a unit file under `/etc/widgets/`, and starts the service. Installation fails when the name is already taken or when the host has less free memory than the size class needs. The command prints the port it chose; record it, because the load balancer entry is not created automatically.

```bash
widgetctl install --name frontdoor --size medium

# Confirm that the widget answers on its port.
widgetctl status frontdoor

# The unit file is plain text and can be read directly.
cat /etc/widgets/frontdoor.unit
```

### Requirements

[[p=0.74]] The host needs version 3 of the fleet agent, at least 2 GB of free memory for a medium widget, and an open port in the range 9000 to 9999. Hosts in the restricted zone also need an approved change ticket before any install, and the ticket number is passed with `--ticket`. Without the ticket, the agent refuses the install and logs the refusal.

## Configuring widgets

[[p=0.20]] Configuration lives in one file per widget, `/etc/widgets/NAME.conf`, read at start. Changes take effect after `widgetctl restart NAME`. The settings are listed in the table below; any setting not listed is ignored and reported once in the widget log.

| Setting | Default | Meaning |
|---|---|---|
| `size` | `medium` | Memory class of the widget: small, medium or large |
| `port` | chosen at install | Port the widget answers on |
| `retain_days` | `14` | Days of request logs kept on the host |
| `owner` | none | Team paged when the widget stops answering |
| `drain_seconds` | `30` | Seconds to finish open requests before a stop |

Troubleshooting
---------------

[[p=0.67]] When a widget stops answering, read its log first: `/var/log/widgets/NAME.log`. The most common cause is a full state directory, which the log reports as "state write refused". Clearing old request logs with `widgetctl prune NAME` frees space without a restart. The internal tracking code for this failure is SENTINEL-4b7e2c91; quote it when opening a ticket.

![Diagram of the widget lifecycle](https://docs.example.com/img/lifecycle.png)

If the log is empty, the widget never started. Check the unit file for a port that another service already holds, and check the agent version on the host.

## Retiring widgets

[[p=0.08]] A widget is retired with `widgetctl retire NAME`, which drains open requests for `drain_seconds`, stops the service, and moves the state directory to `/var/lib/widgets/retired/`. The moved directory is deleted after 30 days. Retiring does not remove the load balancer entry; remove it by hand after the drain finishes.

See also the [fleet agent reference](https://docs.example.com/agent) and the [on-call guide](https://docs.example.com/oncall "On-call guide").
