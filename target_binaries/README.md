# target_binaries/ — prebuilt firmware for the binary-in-the-loop stage

Drop **already-built** Zephyr artifacts here (no toolchain / `west build` needed).
The binary-in-the-loop stage (Phase 3) loads each one headless under Renode,
bounds it by *virtual* time, classifies any failure, and routes re-grounded
fixes back to the engineer. It **complements** the samples-build Tester, which
remains the primary functional gate.

## Layout

```
target_binaries/
  <board>/
    <name>/
      zephyr.elf        # or .hex / .bin
      meta.toml
```

`<board>` must match the board name the pipeline produces in `output/<board>/`.

## meta.toml

```toml
[binary]
board = "bbc_microbit_v2"     # informational
soc = "nRF52833"              # informational
file = "zephyr.elf"           # artifact filename in this directory
format = "elf"                # elf | hex | bin
load_address = 0x0            # REQUIRED for raw .bin only
console_uart = "uart0"        # repl peripheral name for the console (UART file backend)
mcuboot = false               # set true + [partitions] for MCUboot images

[success]
tier = 1                      # 1 = boot + no fault + no spin (default pass bar)
virtual_time_budget_s = 30    # virtual time to run before judging
# Tier-2 (optional) golden criteria — when present, these must also match:
expect_console = ["Hello World! bbc_microbit_v2"]
# expect_exit = "..."
```

## Pass criteria (tiered, configurable)

- **Tier 1 (always):** the firmware boots and runs to the virtual-time budget
  with no unmapped `sysbus` access, no unhandled-register/Tag hit, no CPU spin,
  and no emulator/load error.
- **Tier 2 (when `expect_console` is set):** the golden console markers are also
  matched. If no markers are supplied, tier 1 is the bar.

Configure globally in `tlde.toml` `[binaries]` (`run_loop`, `max_attempts`,
`virtual_time_budget_s`, `default_success_tier`, `renode_bin`, `wall_timeout_s`).
