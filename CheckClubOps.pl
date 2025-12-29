#!/usr/bin/perl
use strict;
use warnings;

# CheckClubOps.pl - Detect club-to-club QSOs in contest logs
#
# Identifies when club operators appear as contacts in their own log.
# Useful for Field Day and multi-operator contests to catch scoring errors.
#
# Input: Cabrillo format contest log via STDIN
# Example OPERATORS line: OPERATORS: W4CU VE3XD KB8ESY N2ESP KC1YL KA4IOX KP2N KR4U KI4UIP NY4I AA0O KA1IJA
# Example QSO line: QSO: 14096 RY 2020-01-04 1800 W4TA 599 FL K1SM 599 MA 0

my @ops;           # Array of operator callsigns
my %opHash;        # Hash for fast operator lookup
my $qsoCount = 0;  # Count of QSOs processed
my $matchCount = 0;# Count of club-to-club QSOs found
my $foundOps = 0;  # Flag: have we found the OPERATORS line?

while (<>) {
   my $line = $_;
   chomp($line);

   # Parse OPERATORS line
   if ($line =~ /^OPERATORS:\s+(.+)$/) {
      my $opList = $1;
      print "Found operators line: $opList\n";
      @ops = split /\s+/, $opList;
      print scalar(@ops) . " operators loaded\n";

      # Build hash immediately for fast lookup
      %opHash = map { $_ => 1 } @ops;
      $foundOps = 1;
   }
   # Parse QSO lines
   elsif ($line =~ /^QSO:/) {
      $qsoCount++;

      # Warn if processing QSOs before finding OPERATORS line
      if (!$foundOps && $qsoCount == 1) {
         warn "Warning: Processing QSO records before OPERATORS line found\n";
      }

      # Split on whitespace (one or more spaces)
      my @fields = split /\s+/, $line;

      # Validate we have enough fields for a proper QSO record
      # Expected format: QSO: freq mode date time mycall myexch1 myexch2 call exch1 exch2 radio
      # Minimum 12 fields (index 0-11)
      if (@fields < 12) {
         warn "Warning: QSO line has insufficient fields (found " . scalar(@fields) . ", expected 12+): $line\n";
         next;
      }

      # Extract contacted callsign (field index 8)
      my $call = $fields[8];

      # Check if contacted station is one of our operators
      if (exists($opHash{$call})) {
         print "Local operator as QSO in our log: $line\n";
         $matchCount++;
      }
   }
}

# Print summary
print "\n=== Summary ===\n";
print "Total QSOs processed: $qsoCount\n";
print "Club-to-club QSOs found: $matchCount\n";

if ($matchCount > 0) {
   print "\nNote: These QSOs may need review depending on contest rules.\n";
}

if (!$foundOps) {
   warn "\nWarning: No OPERATORS line found in input\n";
}
