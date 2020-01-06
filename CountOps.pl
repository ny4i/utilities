#!/usr/bin/perl
# QSO: 14096 RY 2020-01-04 1800 W4TA          599 FL     K1SM          599 MA     0 
my %calls;

sub hashValueDescendingNum {
   $calls{$b} <=> $calls{$a};
}


while (<>) {
   my $line = $_;
   next unless $line =~ /^ <CALL/;
   if ($line =~ /<OPERATOR:\d{1,6}>(.{4,6}) </) {
      $call = $1;
      if (exists($calls{$call})){
         $calls{$call} = $calls{$call} + 1;
      } else {
         $calls{$call} = 1;
      }  
   } else {
      print $line;
   }
}
$cbrRec = "OPERATORS:";
foreach $key (sort hashValueDescendingNum (keys(%calls))) {
   print "\t$calls{$key} \t\t $key\n";
   $cbrRec = $cbrRec . " " . $key;
   
}
 
print "$cbrRec\n";