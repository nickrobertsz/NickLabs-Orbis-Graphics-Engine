/* Host test for ps4_parse_epoch() extracted from kernel patch ge-6.15/0010 (time handoff). */
#include <stdio.h>
#include <errno.h>
#include <string.h>
typedef long long s64;
struct timespec64 { s64 tv_sec; long tv_nsec; };
#define __init
#define PS4_TIME_MIN	1577836800LL
#define PS4_TIME_MAX	4102444800LL
#include "extracted_epoch.inc"

static int fails;
static void ok(const char *s, s64 sec, long nsec)
{
	struct timespec64 t = { -1, -1 };
	int r = ps4_parse_epoch(s, &t);
	if (r || t.tv_sec != sec || t.tv_nsec != nsec) {
		printf("FAIL '%s' -> r=%d %lld.%ld\n", s, r, t.tv_sec, t.tv_nsec); fails++;
	}
}
static void bad(const char *s)
{
	struct timespec64 t = { 0, 0 };
	if (!ps4_parse_epoch(s, &t)) { printf("FAIL accepted '%s'\n", s ? s : "(null)"); fails++; }
}
int main(void)
{
	ok("1793000000", 1793000000LL, 0);
	ok("1793000000.5", 1793000000LL, 500000000L);
	ok("1793000000.000005000", 1793000000LL, 5000L);
	ok("1793000000.1234567891234", 1793000000LL, 123456789L);
	ok("1577836800", 1577836800LL, 0);
	bad("1577836799");          /* before 2020: a lost clock */
	bad("0");
	bad("4102444801");
	bad("");
	bad(NULL);
	bad("17930000x0");
	bad("1793000000.5s");
	bad("-1793000000");
	bad("1234567890123");       /* > 12 digits */
	printf("epoch %s\n", fails ? "FAILED" : "OK");
	return fails != 0;
}
