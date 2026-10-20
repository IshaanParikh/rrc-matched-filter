# Pin assignments for the EPT DueProLogic (EPT-4CE6-AF-D2, USB-C rev),
# EP4CE6E22C8N, sourced by hw_demo/dpl/build.tcl.
#
# Sources, from EPT's DueProLogic 5.1 package:
#   DS  = Documentation/DPL_FPGA_DEV_SYS_DS_V71.pdf (oscillator and LED tables)
#   SCH = Documentation/DPL_USB_FPGA_SCHEMATIC_V7.pdf
#   QSF = Projects_HDL/EPT_4CE6_AF_Platform_Demo/EPT_4CE6_AF_D1_Top/EPT_4CE6_AF_D1_Top.qsf
# Port names match EPT's own top level, so the QSF lines carry over as-is.

# 66 MHz oscillator, GCLK1. DS "Oscillators" table, QSF CLK_66MHZ.
set_location_assignment PIN_23  -to CLK_66MHZ

# active-low reset into the power-on counter. QSF RST. On SCH this is
# CLK5 (pin 89) with R104 2.74k to 3V3, so it reads high unless pulled low.
set_location_assignment PIN_89  -to RST

# FT2232H channel B in UART mode, 115200 8N1, the Active Transfer link.
# QSF UART_IN/UART_OUT. SCH: BDBUS0 (FT TXD) -> FT_BD0 -> pin 43,
# pin 38 -> FT_BD1 -> BDBUS1 (FT RXD).
set_location_assignment PIN_43  -to UART_IN
set_location_assignment PIN_38  -to UART_OUT

# first four LEDs of the 6x6 array (DS "LEDs" table: LED0..3 on UB22..25,
# XIO_6[0..3]). Sunk by the FPGA through 220R to 3V3, so low = on. Each
# block of LEDs has a power-enable jumper that must be fitted to see them.
set_location_assignment PIN_115 -to LED[0]
set_location_assignment PIN_114 -to LED[1]
set_location_assignment PIN_113 -to LED[2]
set_location_assignment PIN_112 -to LED[3]

# every user I/O on the board is 3.3 V (DS "Inputs/Outputs")
foreach port {CLK_66MHZ RST UART_IN UART_OUT LED[0] LED[1] LED[2] LED[3]} {
    set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to $port
}
