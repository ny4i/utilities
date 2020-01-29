#!/usr/bin/perl
# QSO: 14096 RY 2020-01-04 1800 W4TA          599 FL     K1SM          599 MA     0 
my %info;
my %info2;

while (<>) {
   my $line = $_;
   if ($line =~ /^QSO/) {
      #print $_;
      my ($header,$freq,$mode,$qsoDate,$qsoTime,$myCall,$myExch1, $myExch2,$call,$exch1,$exch2,$radio) = split /\s{1,}/, $line;
      print "$call has bad class ($exch1) \n" unless ($exch1 =~ /^\d{1,2}[IHO]$/);
      
      
      if ($exch2 =~ /^[+-]?\d+$/ ) {
         print "DX?\t$call\t$exch2\n";
      } else {
         # Not a number so check if valid abbreviation
         if ($exch2 =~ /^(DX|CT|EMA|ME|NH|RI|VT|WMA|ENY|NLI|NNJ|NNY|SNJ|WNY|DE|EPA|MDC|WPA|AL|GA|KY|NC|NFL|SC|SFL|WCF|TN|VA|PR|VI|AR|LA|MS|NM|NTX|OK|STX|WTX|EB|LAX|ORG|SB|SCV|SDG|SF|SJV|SV|PAC|AZ|EWA|ID|MT|NV|OR|UT|WWA|WY|AK|MI|OH|WV|IL|IN|WI|CO|IA|KS|MN|MO|NE|ND|SD|MAR|NL|QC|ONN|ONS|ONE|GTA|MB|SK|AB|BC|NT)$/) {
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
         
         # Add to hash
         if (exists($info2{$call})) {
            if ($info2{$call} ne $exch1){
               print "Prior call record for $call had exchange of $info2{$call} - current exchange = $exch1\n";
            }
         }
         else {
            $info2{$call} = $exch1;
         }
      }
   }
}


# States if ($exch2 =~ /^(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|P[AR]|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY]|ON|AB|SK|BC|QC|NS|MB)$/) {
        
# Sections if ($exch2 =~ /^(CT|EMA|ME|NH|RI|VT|WMA|ENY|NLI|NNJ|NNY|SNJ|WNY|DE|EPA|MDC|WPA|AL|GA|KY|NC|NFL|SC|SFL|WCF|TN|VA|PR|VI|AR|LA|MS|NM|NTX|OK|STX|WTX|EB|LAX|ORG|SB|SCV|SDG|SF|SJV|SV|PAC|AZ|EWA|ID|MT|NV|OR|UT|WWA|WY|AK|MI|OH|WV|IL|IN|WI|CO|IA|KS|MN|MO|NE|ND|SD|MAR|NL|QC|ONN|ONS|ONE|GTA|MB|SK|AB|BC|NT)$/) {