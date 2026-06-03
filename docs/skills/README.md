# Renode Firmware Emulation Skills

A collection of LLM-agent skills for generating Renode emulation artifacts. These skills provide structured knowledge and patterns for creating platform descriptions, scripts, tests, and peripheral models for the [Renode](https://renode.io/) open-source simulation framework.

## Skills Overview

| Skill | File | Use When |
|-------|------|----------|
| [REPL Generation](renode-repl-generation.md) | `renode-repl-generation.md` | Defining hardware platform topology (CPUs, memory, peripherals, interrupts) |
| [RESC Generation](renode-resc-generation.md) | `renode-resc-generation.md` | Writing scripts to configure and launch emulation scenarios |
| [Robot Test Generation](renode-robot-test-generation.md) | `renode-robot-test-generation.md` | Creating automated firmware tests with Robot Framework |
| [Peripheral Model Generation](renode-peripheral-model-generation.md) | `renode-peripheral-model-generation.md` | Implementing C# peripheral models (registers, IRQs, base patterns) |
| [Peripheral Model Patterns](renode-peripheral-model-patterns.md) | `renode-peripheral-model-patterns.md` | Advanced peripheral templates (GPIO, SPI, DMA, Watchdog, sensors) |
| [Debugging](renode-debugging.md) | `renode-debugging.md` | Diagnosing emulation failures, unmapped accesses, boot hangs, IRQ issues |
| [Zephyr DTS Analysis](zephyr-dts-analysis.md) | `zephyr-dts-analysis.md` | Extracting peripheral configuration from Zephyr build artifacts |
| [MCUboot Emulation](mcuboot-emulation.md) | `mcuboot-emulation.md` | Setting up secure bootloader emulation, partition layouts, firmware updates |
| [Feedback Schemas](renode-feedback-schema.md) | `renode-feedback-schema.md` | verification_report.json / doubt_log.json / revision & binary-loop feedback contracts; grounding via tlde-kb |

## Typical Workflow

A complete Renode emulation setup follows this order:

```
1. Peripheral Model (.cs)   → Implement hardware behavior in C#
2. Platform Description (.repl) → Describe hardware topology and connections
3. Script (.resc)               → Configure machine, load firmware, set up environment
4. Test (.robot)                → Verify firmware runs correctly under emulation
```

## How to Use These Skills

### As Context for an LLM Agent

Include the relevant skill file(s) as context when prompting an LLM to generate Renode artifacts. For example:

- **"Create a REPL for an STM32F4 with 2 UARTs, 3 timers, and GPIO"** → use `renode-repl-generation.md`
- **"Write a script to boot Zephyr on nRF52840 with BLE"** → use `renode-resc-generation.md`
- **"Generate a Robot test that verifies UART output"** → use `renode-robot-test-generation.md`
- **"Model a custom SPI peripheral with interrupt support"** → use `renode-peripheral-model-generation.md` + `renode-peripheral-model-patterns.md`
- **"Debug why the firmware hangs at boot"** → use `renode-debugging.md`
- **"Extract peripheral addresses from a Zephyr build"** → use `zephyr-dts-analysis.md`
- **"Set up MCUboot with A/B slots for firmware updates"** → use `mcuboot-emulation.md`

### Combining Skills

Most real tasks require multiple skills. For example, adding a new peripheral to Renode requires:

1. **Peripheral Model** → Write the C# implementation
2. **REPL** → Register the peripheral at its memory-mapped address with IRQ connections
3. **RESC** → Load the platform and firmware binary
4. **Robot Test** → Verify the peripheral works with real firmware

### Skill Selection Guide

```
Need to define hardware addresses and IRQ wiring?  → REPL skill
Need to load firmware and start emulation?          → RESC skill
Need to verify firmware behavior automatically?     → Robot Test skill
Need to implement register-level peripheral logic?  → Peripheral Model skill(s)
Need to diagnose boot hangs or unmapped accesses?   → Debugging skill
Need to extract peripheral info from Zephyr build?  → Zephyr DTS Analysis skill
Need to emulate MCUboot-based firmware?             → MCUboot Emulation skill
```

## Reference Material

These skills were derived from the upstream Renode sources and documentation:

| Resource | Contents |
|----------|----------|
| [Renode repository](https://github.com/renode/renode) | Main Renode repository (platforms, scripts, tests) |
| [Renode documentation](https://renode.readthedocs.io/en/latest/) | Official Renode documentation |
| [Renode infrastructure source](https://github.com/renode/renode-infrastructure) | Core C# infrastructure (peripheral base classes, register framework) |
| [peakrdl-renode tool](https://github.com/renode/renode/tree/master/tools/PeakRDL-renode) | Automatic peripheral stub generation from SystemRDL |

## Quick Reference

### File Extensions

| Extension | Purpose | Example |
|-----------|---------|---------|
| `.repl` | Platform description | `platforms/cpus/stm32f4.repl` |
| `.resc` | Monitor script | `scripts/single-node/stm32f4_discovery.resc` |
| `.robot` | Robot Framework test | `tests/platforms/STM32F4Discovery.robot` |
| `.cs` | C# peripheral model | `Peripherals/UART/STM32_UART.cs` |

### Key Renode Commands

```bash
# Launch Renode with a script
renode script.resc

# Run automated tests
renode-test test.robot

# Run tests in parallel
renode-test -j4 -t tests.yaml
```

### Peripheral Type Namespaces

```
CPU.              → Processors (CortexM, ARMv8A, RiscV32, ...)
UART.             → Serial ports
Timers.           → Timer/counter peripherals
IRQControllers.   → NVIC, GIC, PLIC, CLINT
GPIOPort.         → GPIO controllers
Memory.           → MappedMemory, ArrayMemory
SPI.              → SPI controllers
I2C.              → I2C controllers
Sensors.          → I2C/SPI sensor devices
DMA.              → DMA controllers
Miscellaneous.    → LED, Button, CombinedInput
Network.          → Ethernet, WiFi controllers
```
