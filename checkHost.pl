#!/usr/bin/perl
# Thomas M. Schaefer, NY4I C. 2020

use strict;
use Net::DNS::Nslookup;
use Net::Telnet;
use POSIX qw(strftime);

 
my $recCount = 0;
my $good = 0;
my $notFound = 0;
my $found = 0;
my $noConnect = 0;
my $verbose = 0;
my $date = strftime "%Y-%m-%d", localtime;

my $dxFileName = "DXCLUSTERS.DAT.$date";
my $trFileName = "trcluster.dat.$date";

open (TRFILE, ">> $trFileName") || die "problem opening $trFileName\n";
open (DXFILE, ">> $dxFileName") || die "problem opening $dxFileName\n";


while (<>) {
   $recCount++;
   my $line = $_;
   if (/"(.*)".*,.*"(.*)".*,.*"(.*)".*,.*"(.*)"/) { # Should add IPv6 support too.
      my $name = $1;
      my $hostname = $2;
      my $port = $3;
      my $type = $4;
      
      print "$name,$hostname,$port,$type\n" if $verbose;
      if ($hostname =~ /VERSION/){
         next;
      } 
      if ($hostname =~ / /){
         print "Invalid hostname;$name;$hostname;$port;$type\n";
         $notFound++;
         next;
      }
      if ($hostname =~ /\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/){ # Check for an IP address so we do not do an nslookup
      } else {
         my $nslookup = Net::DNS::Nslookup->get_ips($hostname);
         if (length($nslookup) == 0){
            print "Invalid Host;$name;$hostname;$port;$type\n";
            $notFound++;
            next;
         }
      }
     
      $found++;
      # If we are still here, the host/IP is valid so check if we can connect
      print "Connecting to $hostname:$port\n" if $verbose;
      my $port1 = Net::Telnet->new( Host=>$hostname,Port=>$port, Timeout => 5, Errmode => 'return');
      if ($port1) {
         print DXFILE "\"$name\",\"$hostname\",\"$port\",\"$type\"\n";
         print TRFILE "$hostname:$port,$type\n";
         $good++;
      } else {
         print "Connect Error;$name;$hostname;$port;$type\n";
         print DXFILE "\"$name\",\"$hostname\",\"$port\",\"$type\"\n";
         $noConnect++
      }
   }
}

print "#Not found = $notFound\n";
print "#Found = $found\n";
print "#No connect = $noConnect\n";
print "#Found = $good\n";
print "total records = $recCount\n";

print "# % bad hostnames = " . (($notFound / $recCount) * 100) . " % \n";
print "#    % No connect = "    . (($noConnect / $recCount) * 100) . " % \n";
print "#          % good = "    . (($good / $recCount) * 100) . " % \n";
