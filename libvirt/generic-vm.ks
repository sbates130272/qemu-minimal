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

# Language modules to install
langsupport en_US

# System keyboard
keyboard us

# System mouse
mouse

# System timezone
timezone --utc Canada/Mountain

# Root password
rootpw --disabled

# Initial user (plaintext password...))
user generic --fullname "Generic" --password change
preseed user-setup/allow-password-weak boolean true

# pick only one of these actions to take after installation completed
reboot

# Use text mode install
text

# Install OS instead of upgrade
install

# Use http installation media
url --url http://archive.ubuntu.com/ubuntu

# System bootloader configuration
bootloader --location=mbr

# Clear the Master Boot Record
zerombr yes

# Partition clearing information
clearpart --all --initlabel

# Partition setup
part / --fstype ext4 --size 1 --grow

# If you have swap commented out/not specified then you need to have this line.
preseed --owner partman-basicfilesystems partman-basicfilesystems/no_swap boolean false

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
@ ubuntu-server
openssh-server
acpid
libpam-systemd
dbus
byobu
vim
build-essential
git
grep
make
sudo
fio
pciutils
tmux
emacs25-nox
sysstat
htop
bmon
nfs-common

# Install some tools that are not packaged and setup our LDAP and NFS
# is available. Also fix the console on grub and set the eideticom
# nameserver for DNS.

%post --nochroot --log=/target/root/test.log
(
cp /hostname.tmp /target/root/
) 1> /target/root/post_install.log 2>&1

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

