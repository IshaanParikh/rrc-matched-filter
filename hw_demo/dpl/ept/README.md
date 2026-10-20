# EPT Active Transfer library goes here

`dpl_top.v` talks to the PC through Earth People Technology's Active Transfer
library. It is EPT's copyrighted code, shipped on the DueProLogic package, so
it is not committed. Copy it in before building or simulating:

```
hw_demo/dpl/ept/src/
    active_serial_library.v   UART flavour of the library (USB-C board rev)
    active_transfer_uart.v
    ft_245_state_machine.v
    endpoint_registers.v
    active_trigger.v          trigger endterm
    active_transfer.v         single-byte endterm
    uart_top.v
    uart_receiver.v
    uart_transmitter.v
    eptWireOR.v
    define.v                  included by the others as "../src/define.v"
```

Take them from the 5.1 package:

```
DUEPROLOGIC_USB_FPGA_PROJECT_5.1_DVD/Projects_HDL/EPT_4CE6_AF_Platform_Demo/src/
```

From Git Bash at the repo root:

```bash
EPT_SRC="/c/Users/<you>/Documents/GFPGA/EPT_DPL/DVD_5.1/DUEPROLOGIC_USB_FPGA_PROJECT_5.1_DVD/Projects_HDL/EPT_4CE6_AF_Platform_Demo/src"
mkdir -p hw_demo/dpl/ept/src
for f in active_serial_library active_transfer_uart ft_245_state_machine \
         endpoint_registers active_trigger active_transfer uart_top \
         uart_receiver uart_transmitter eptWireOR define; do
    cp "$EPT_SRC/$f.v" hw_demo/dpl/ept/src/
done
```

The folder has to be called `src`. Every library file does
`` `include "../src/define.v" ``, which only resolves from a sibling `src`
directory.

Use the `active_serial_library` version, not the older
`active_transfer_library` from the user manual. The USB-C board runs the
FT2232H as a UART, and the serial library wraps the same FT245-style core
behind it, so the endterms (`active_trigger`, `active_transfer`,
`active_block`) and the `UC_IN`/`UC_OUT` buses are unchanged.

Everything in this folder except this README is ignored by git.
