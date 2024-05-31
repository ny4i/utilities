# Sample program to broadcast to all NICs to find any K4s on the network
# Tom Schaefer, NY4I
# ny4i@ny4i.com
# May 2024

import socket

#Constants

UDP_SEND_PORT = 9100 # UDP port the K4 listens on for the findk4 byte string 
MESSAGE = b"findk4" # A byte string hence the b" "
SOCKET_TIMEOUTV = 3
K4_RETURN_START = b'k4'
RETURN_DELIMITER = ":"

verbose = False # Set to true for more detailed display messages
done = False
nK4Count = 0


interfaces = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) # AF_INET specifies only to return IPv4-capable interfaces
#interfaces = socket.getaddrinfo(host=socket.gethostname(), port=None, family=socket.AF_INET) # AF_INET specifies only to return IPv4-capable interfaces
IPList = [ip[-1][0] for ip in interfaces]
if verbose:
   for ip in IPList:
      print("Interface found with IP address %s" % ip)


if verbose:
   print("message: %s" % MESSAGE)



for ip in IPList: # This code sends to all interfaces on this system
   if verbose:
      print("Sending message to network via NIC with IP address %s" % ip)
   sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)  # UDP
   sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
   sock.bind((ip,0)) # This sets the interface to which we broadcast
   (lhost, lport) = portInfo = sock.getsockname()  # This code grabs the random source port number assigned when we send the message
   if verbose:                                     # The port we send on is the port to which the K4 will reply so we have to listen on this port later
      print ("lhost: %s" % lhost)
      print ("lport: %d" % lport)
   sock.sendto(MESSAGE, ("255.255.255.255", UDP_SEND_PORT)) # Even though this state IP 255.255.255.255, we broadcast to the ip set in the sock.bind above
   sock.close()
   sock_rec = socket.socket(socket.AF_INET, # Internet
                            socket.SOCK_DGRAM) # UDP
   sock_rec.settimeout(SOCKET_TIMEOUTV) # Wait max SOCKET_TIMEOUTV seconds for reply
   sock_rec.bind(('', lport))
   if verbose:
      print("Bound to receive socket - Now waiting for input on port %d" % lport)
      
   # Expecting a colon-delimited string that looks like this:  b'k4:0:192.168.73.108:278' with 
   # k4 first, then the ID, then the IP address then the serial number 
   while not done:   # Use while loop to handle multiple replies from same NIC in case multiple K4s on one network
      try:
         data, addr = sock_rec.recvfrom(4096) # buffer size is 1024 bytes
         if verbose:
            print("Received message: %s from address %s" % (data, addr)) #'k4:0:192.168.73.108:278
         if verbose: 
            print (type(data))
         if data.startswith(K4_RETURN_START): # Check if this starts with k4 to avoid doing the split code needlessly for unexpected data
            sData = data.decode("utf-8") # Convert bytes returned in recvfrom to a string
            (sRigType, sRigIndex, sRigIP, sRigSN) = sData.split(RETURN_DELIMITER)  # Split into strings rather than a list for ease of reference
            if verbose:
               print (sData.split(RETURN_DELIMITER))
            sSNFull = sRigSN.zfill(5) # Zero pad the string without converting to a number
            print ("Found K4 serial number %s at IP address %s (K4-SN%s.local)" % (sRigSN, sRigIP, sSNFull))
            nK4Count += 1
         else:
            print("Unexpected data received from %s: %s" % (addr, data))
      except socket.timeout:
         done = True
   sock_rec.close()
   done = False
   
if nK4Count > 0:
   print("Found %d K4 %s accessible to this computer" % (nK4Count, "radio" if nK4Count == 1 else "radios"))
else:
   print ("No K4 radios found")
   




    
  
  