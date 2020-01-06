#!/usr/bin/perl

# QSO: 14096 RY 2020-01-04 1800 W4TA          599 FL     K1SM          599 MA     0 

# OPERATORS: W4CU VE3XD KB8ESY N2ESP KC1YL KA4IOX KP2N KR4U KI4UIP NY4I AA0O KA1IJA

my %info;

while (<>) {
   my $line = $_;
   if ($line =~ /^OPERATORS: (.*)$/) {
       print "Found operators line $1\n";
       (@ops) = split /\s{1,}/, $1;
       print scalar @ops . " operators loaded\n";
   } elsif ($line =~ /^QSO/) {
      $qsoCount++;
      if ($qsoCount == 1) {
         # First time so load ops hash
         print "Loading operator hash\n";
         $opHash{$_}++ for (@ops);
      }
      #print $_;
      my ($header,$freq,$mode,$qsoDate,$qsoTime,$myCall,$myExch1, $myExch2,$call,$exch1,$exch2,$radio) = split /\s{1,}/, $line;
      if (exists($opHash{$call})){
         print "Local operator as QSO in our log: $line\n";
      }
   }
}