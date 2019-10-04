# generic-vm.ks
# ---------------
#
# (c) Raithlin, Inc, 2018
#
# A kickstart file to setup genric VMs withstandard packaging
# and tooling. Also runs post install scripts to fix grub console
# access.
#

# System language
lang en_US

# System keyboard
keyboard us

# System timezone
timezone --utc Canada/Mountain

# Root password
rootpw --lock

# Initial user (plaintext password...))
user --name generic --gecos "Generic" --password change

# pick only one of these actions to take after installation completed
reboot

# Use text mode install
text

# Install OS instead of upgrade
install

# Use http installation media
url --url http://mirror.i3d.net/pub/centos/7/os/x86_64/

# System bootloader configuration
bootloader --location=mbr

# Clear the Master Boot Record
zerombr

# Partition clearing information
clearpart --all --initlabel

# Partition setup
part / --fstype ext4 --size 1 --grow

# System authorization infomation
auth  --useshadow  --enablemd5

# Firewall configuration
firewall --disabled

# Do not configure the X Window System
skipx

# Make sure to install the acpid package so that virsh commands such
# as virsh shutdown will take effect
# @ is for groups - http://landoflinux.com/linux_kickstart_keywords.html    
%packages
tmux
%end


# Install some tools that are not packaged and setup our LDAP and NFS
# is available. Also fix the console on grub and set the eideticom
# nameserver for DNS.

%post --nochroot --log=/target/root/test.log
(
cp /hostname.tmp /target/root/
) 1> /target/root/post_install.log 2>&1
%end

%post
(

sed -i "s;quiet;quiet console=ttyS0;" /etc/default/grub
update-grub

git clone -b eid-plugin-152131 --recurse-submodules https://github.com/Eideticom/nvme-cli.git
cd nvme-cli
git submodule update --init
make install
cd ..
rm -rf nvme-cli

cp /root/hostname.tmp /etc/hostname

) 1> /root/post_install-step2.log 2>&1
%end
