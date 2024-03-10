#!/usr/bin/perl
# QSO: 14096 RY 2020-01-04 1800 W4TA          599 FL     K1SM          599 MA     0 
my %info;

while (<>) {
   my $line = $_;
   if ($line =~ /^QSO/) {
      #print $_;
      my ($header,$freq,$mode,$qsoDate,$qsoTime,$myCall,$myExch1, $myExch2,$call,$exch1,$exch2,$radio) = split /\s{1,}/, $line;
      if ($exch2 =~ /^[+-]?\d+$/ ) {
         #print "DX?\t$call\t$exch2\n";
      } else {
         # Not a number so check if valid abbreviation
         if ($exch2 =~ /^(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|P[AR]|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY]||AB|BC|GH|MB|NB|NL|NS|ONE|ONN|ONS|PE|QC|SK|TER)$/) {
         } else {
            print "$call\t $exch2 not valid\n";
         }
         # Add to hash
         if (exists($info{$call})) {
            if ($info{$call} ne $exch2){
               print "Prior call record for $call had exchange of $info{$call} - current exchange = $exch2\n";
            }
         }
         else {
            $info{$call} = $exch2;
         }
      }
   }
}
