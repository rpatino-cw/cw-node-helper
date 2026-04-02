# cwhelper

DCT terminal companion -- Jira + NetBox + Grafana queue browser for data center operations.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Version](https://img.shields.io/badge/version-6.3.0-orange)
![License](https://img.shields.io/badge/license-MIT-green)

---

<!-- TODO: record hero.gif — full flow: launch → queue → pick ticket → detail view (~10s) -->
![cwhelper demo](assets/hero.gif)

---

## Features

<table>
<tr>
<td width="50%">

<!-- TODO: record queue.gif — filter by status, scroll, site picker -->
![Queue Browser](assets/queue.gif)

**Queue Browser** -- Filter, sort, and navigate your Jira queue with keyboard shortcuts.

</td>
<td width="50%">

<!-- TODO: record detail.gif — ticket header, hotkey panel, field display -->
![Ticket Detail](assets/detail.gif)

**Ticket Detail** -- Where, what, which device -- answered at a glance with full hotkey panel.

</td>
</tr>
<tr>
<td width="50%">

<!-- TODO: record rack.gif — press [r], ASCII rack + DH mini-map -->
![Rack Map](assets/rack.gif)

**Rack Map** -- ASCII rack visualization and data hall mini-map. See your position in the floor.

</td>
<td width="50%">

<!-- TODO: record cab.gif — [ws] grab waiting, [hg] give cab -->
![Cab Workflow](assets/cab.gif)

**Cab Workflow** -- Grab waiting tickets, give or take a whole cabinet in one move.

</td>
</tr>
</table>

---

## Install

```bash
git clone https://github.com/rpatino-cw/cw-node-helper.git
cd cw-node-helper
pip install -e .
cp .env.example .env   # fill in your Jira + NetBox tokens
cwhelper
```

## Hotkeys

| Key | Action |
|-----|--------|
| `q` | Queue browser |
| `s` | Search tickets (JQL) |
| `b` | Bookmarks |
| `r` | Rack map + DH mini-map |
| `c` | Connections (HO/MRB/SDx) |
| `n` | NetBox device lookup |
| `g` | Grab ticket |
| `ws` | Grab waiting tickets in cab |
| `hg` | Give cab to teammate |
| `lg` | Link grab (bulk) |
| `w` | Watch mode (background) |
| `p` | Start all (own queue) |
| `h` | History |
| `?` | Help |

---

MIT
