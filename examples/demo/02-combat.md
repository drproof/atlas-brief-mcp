---
id: combat
title: Combat System
status: active
tags: [combat, balancing]
implements: [scripts/combat/combat_system.gd]
aliases: [KS, Kampf, Kampfsystem, fighting]
---

# Combat System

Turn-based, but without an initiative bar: whoever acted last, acts last again.

## Damage model

Base weapon damage minus armour value, minimum 1. Cold adds 10 percent damage
per level, because fingers get stiff.

| Weapon | Base damage | Range |
|---|---|---|
| Axe | 7 | 1 |
| Spear | 5 | 2 |
| Bow | 4 | 4 |

## Cover

Cover halves incoming damage. Flanking ignores cover entirely.

- [ ] Test the damage formula against cold levels @balancing #combat
- [ ] Move the weapon table into a data resource @code #data
- [x] First cover system prototyped

## Morale in combat

If morale drops below 30, settlers with a low courage score flee.
