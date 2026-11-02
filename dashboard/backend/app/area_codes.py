"""US area code -> IANA time zone, for calling hours in the lead's local time.

Area codes that span two zones use the zone most of the area is in (e.g. 850 Florida panhandle
-> Central, 915 El Paso -> Mountain). Unknown area codes fall back to Eastern.
"""

from zoneinfo import ZoneInfo

_ZONES: dict[str, str] = {
    "America/New_York": (
        # CT DE DC FL GA IN(most) KY(east) ME MD MA MI NH NJ NY NC OH PA RI SC TN(east) VT VA WV
        "203 475 860 959 302 202 771 239 305 321 324 352 386 407 448 561 645 656 689 727 728 754 772 786 813 863 904 941 954 "
        "229 404 470 478 678 706 762 770 912 943 260 317 463 574 765 812 930 502 606 859 207 227 240 301 410 443 667 "
        "339 351 413 508 617 774 781 857 978 231 248 269 313 517 586 616 679 734 810 947 989 603 201 551 609 640 732 848 "
        "856 862 908 973 212 315 329 332 347 363 516 518 585 607 631 646 680 716 718 838 845 914 917 929 934 252 336 472 704 743 "
        "828 910 919 980 984 216 220 234 283 326 330 380 419 436 440 513 567 614 740 937 215 223 267 272 412 445 484 570 582 610 "
        "717 724 814 835 878 401 803 821 839 843 854 864 423 865 802 276 434 540 571 686 703 757 804 826 948 304 681 "
    ),
    "America/Chicago": (
        # AL AR IL IA KS LA MN MS MO NE ND OK SD TN(mid/west) TX(most) WI, FL panhandle, IN NW, KY west
        "205 251 256 334 483 659 938 327 479 501 870 217 224 309 312 331 447 464 618 630 708 730 773 779 815 847 861 872 "
        "319 515 563 641 712 316 620 785 913 225 318 337 457 504 985 218 320 507 612 651 763 924 952 228 601 662 769 "
        "314 417 557 573 636 660 816 975 308 402 531 701 405 539 572 580 918 605 615 629 731 901 931 "
        "210 214 254 281 325 346 361 409 430 432 469 512 682 713 726 737 806 817 830 832 903 936 940 945 956 972 979 "
        "262 274 353 414 534 608 715 920 850 219 270 364 "
    ),
    "America/Denver": "303 719 720 970 983 208 986 406 505 575 385 435 801 307 915 ",
    "America/Phoenix": "480 520 602 623 928 ",  # Arizona: no daylight saving time
    "America/Los_Angeles": (
        "209 213 279 310 323 341 350 357 369 408 415 424 442 510 530 559 562 619 626 628 650 657 661 669 707 714 738 747 "
        "760 805 818 820 831 837 840 858 909 916 925 949 951 702 725 775 458 503 541 971 206 253 360 425 509 564 "
    ),
    "America/Anchorage": "907 ",
    "Pacific/Honolulu": "808 ",
}

AREA_TZ: dict[str, str] = {code: tz for tz, codes in _ZONES.items() for code in codes.split()}
DEFAULT_TZ = "America/New_York"


def tz_for_phone(e164: str) -> tuple[str, bool]:
    """(IANA zone, known?) for a +1NXXNXXXXXX number."""
    area = e164[2:5] if e164.startswith("+1") and len(e164) == 12 else ""
    tz = AREA_TZ.get(area)
    return (tz, True) if tz else (DEFAULT_TZ, False)


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)
