# Manual merge notes: Baikal hunks in files the 6.15.4 GE tree already has

Source: sony-jaguar-devs/ps4-linux@5.4.247-baikal-dfaus (7f796666a956). These are NOT candidates: a whole-file
replacement would revert 6.15/GE changes. Each Baikal hunk must be merged by hand, then added with
`portforge candidate --origin manual`.

## arch/x86/include/asm/ps4.h (5.4 lines mentioning bpcie/baikal; 6.15 tree has 0 such lines)
```
19:#define EMC_TIMER_BASE (BCPIE_BAR4_ADDR + 0x9000) //BAR4 + 0x9000, seems this is not HPET timer, Baikal WDT
49://Baikal		 
50:extern int bpcie_assign_irqs(struct pci_dev *dev, int nvec);
51:extern void bpcie_free_irqs(unsigned int virq, unsigned int nr_irqs);
53:extern int bpcie_status(void);
54:extern int bpcie_icc_cmd(u8 major, u16 minor, const void *data,
78://Baikal
79:static inline int bpcie_assign_irqs(struct pci_dev *dev, int nvec)
84:static inline void bpcie_free_irqs(unsigned int virq, unsigned int nvec)
88:static inline int bpcie_status(void)
92:static inline int bpcie_icc_cmd(u8 major, u16 minor, const void *data,
```

## arch/x86/platform/ps4/ps4.c (5.4 lines mentioning bpcie/baikal; 6.15 tree has 0 such lines)
```
35:bool bpcie_initialized;
38: * The RTC is part of the Aeolia/Baikal PCI device and will be implemented there as
58:	return apcie_initialized || bpcie_initialized;
62:int bpcie_status(void)
66:	return bpcie_initialized;
68:EXPORT_SYMBOL_GPL(bpcie_status);
```

## drivers/ps4/Makefile (5.4 lines mentioning bpcie/baikal; 6.15 tree has 0 such lines)
```
1:# Aeolia and Baikal PCI devices
10:obj-y += ps4-bpcie.o \
11:	ps4-bpcie-uart.o \
12:	ps4-bpcie-icc.o \
```

## drivers/usb/host/xhci-aeolia.c (5.4 lines mentioning bpcie/baikal; 6.15 tree has 4 such lines)
```
223:			ctlr->apcie_bpcie_buffer = (void *)0x04;//for ahci 0x024
224:			bpcie_sata_phy_init(&pdev->dev, ctlr);
396: 		if(dev->device != PCI_DEVICE_ID_SONY_AEOLIA_XHCI && idx == 1) {//this is for Belize and Baikal
473:		{ PCI_DEVICE(PCI_VENDOR_ID_SONY, PCI_DEVICE_ID_SONY_BAIKAL_XHCI) },
```

## drivers/ata/ahci.c (5.4 lines mentioning bpcie/baikal; 6.15 tree has 106 such lines)
```
50:        AHCI_PCI_BAR0_BAIKAL	= 0,
590:	{ PCI_VDEVICE(SONY, PCI_DEVICE_ID_SONY_BAIKAL_AHCI), board_ahci },
1748:        else if (pdev->vendor == PCI_VENDOR_ID_SONY && pdev->device == PCI_DEVICE_ID_SONY_BAIKAL_AHCI)
1749:			ahci_pci_bar = AHCI_PCI_BAR0_BAIKAL;
1965:void bpcie_sata_phy_init(struct device *dev, struct ahci_controller *ctlr)
2015:  int bpcie_buffer; // eax
2389:  dev_info(dev, "Baikal SATA PHY init\n");
2390:  struct pci_dev* sc_dev = pci_get_device(PCI_VENDOR_ID_SONY, PCI_DEVICE_ID_SONY_BAIKAL_PCIE, NULL);
2392:	  dev_err(dev, "bpcie glue: not device found\n");
2397:	  dev_err(dev, "bpcie glue: not ready yet\n");
2403:    bpcie_write_to_bar2_and_0x180000_and_offset(108LL, 1u);
2404:    bpcie_write_to_bar2_and_0x180000_and_offset(44LL, 1u);
2409:    bpcie_write_to_bar2_and_0x180000_and_offset(112LL, 1u);
2410:    bpcie_write_to_bar2_and_0x180000_and_offset(48LL, 1u);
2413:  bpcie_write_to_bar2_and_0x180000_and_offset(v9, 0);
2414:  v10 = bpcie_read_from_bar4_and_0xC000_and_offset(72LL);
2415:  v11 = bpcie_read_from_bar4_and_0xC000_and_offset(108LL);
2441:  dev_info(dev, "Baikal SATA EFUSE VALUE: 0x%02x:0x%02x:0x%02x:0x%02x:0x%02x:0x%02x\n", v16, v17, 0, 0, 0, 0);
2446:  v22 = bpcie_ahci_read(ctlr->r_mem, 0x20A0);
2449:  bpcie_ahci_write(ctlr->r_mem, 0x20A0, (v22 & 0xFBFF03FF) | ((bpci_usb_ahci ? v423 : v422) << 10) | 0x4000000);
2451:  v29 = bpcie_ahci_read(ctlr->r_mem, 0x2014);
2452:  bpcie_ahci_write(ctlr->r_mem, 0x2014, v29 | 0x100000);
2454:  v36 = bpcie_ahci_read(ctlr->r_mem, 0x2054);
2455:  bpcie_ahci_write(ctlr->r_mem, 0x2054, v36 & (0xFFFFF07F | ((bpci_usb_ahci ? v421 : v420) << 7)));
2457:  v42 = bpcie_ahci_read(ctlr->r_mem, 0x201C);
```
