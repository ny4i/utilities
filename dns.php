#!/usr/bin/php 
<?php

function IsValidDNS ($domain) : bool {
    
   if (checkdnsrr($domain,"A")) {
      return true;
   } else {
      return false;
   }
}

// Test two hosts from file
// Valid;S50DXS;s50dxs.s53m.com;8000;DX Spider
// Invalid Host;S55HHH-1;cluster.s55hhh.net;7373;AR-Cluster
$domain = "s50dxs.s53m.com";
if (IsValidDNS($domain)) {
   echo "$domain is valid\n";
} else {
   echo "$domain not valid\n";
}

// Invalid Host;S55HHH-1;cluster.s55hhh.net;7373;AR-Cluster
$domain = "cluster.s55hhh.net";
if (IsValidDNS($domain)) {
   echo "$domain is valid\n";
} else {
   echo "$domain not valid\n";
}



?>
