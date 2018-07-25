# eideticom-vm.ks
# ---------------
#
# (c) Eideticom, Inc, 2018
#
# A kickstart file to setup Eideticom VMs with our standard packaging
# and tooling. Also runs post install scripts to fix grub console
# access and setup NFS and LDAP.
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

# Initial user (hashed password with salt sb...))
user eidetic --fullname "Eideticom" --password sbkeBMPDdj/6w --iscrypted 
preseed user-setup/allow-password-weak boolean true

# pick only one of these actions to take after installation completed
reboot
#shutdown
#halt
#poweroff

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
#part /boot --fstype ext2 --size 200 --asprimary
#part swap  --size 1024
#part pv.01 --size 1 --grow
#volgroup rootvg pv.01
#logvol / --fstype ext4 --vgname=rootvg --size=1 --grow --name=rootvol
#preseed partman-lvm/confirm_nooverwrite boolean true

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
# Packages for Eideticom NoLoad development
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
cp /ldap.conf /target/etc/
cp /ldap.secret /target/etc/

) 1> /target/root/post_install.log 2>&1

%post
(
ls /media/cdrom/
cp /media/cdrom/ldap.conf /etc/
cp /media/cdrom/ldap.secret /etc/

sed -i "s;quiet;quiet console=ttyS0;" /etc/default/grub
update-grub

git clone -b eid-plugin-152131 --recurse-submodules https://github.com/Eideticom/nvme-cli.git
cd nvme-cli
git submodule update --init
make install
cd ..
rm -rf nvme-cli

echo "      nameservers:" >> /etc/netplan/01-netcfg.yaml
echo "        search: [eideticom]" >> /etc/netplan/01-netcfg.yaml
echo "        addresses: [192.168.6.1]" >> /etc/netplan/01-netcfg.yaml
netplan apply /etc/netplan/01-netcfg.yaml

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y libnss-ldap nscd
#dpkg-reconfigure ldap-auth-config
auth-client-config -t nss -p lac_ldap
pam-auth-update

mkdir -p /home/users
echo "hades:/users        /home/users     nfs auto,x-systemd.automount,x-systemd.device-timeout=10,timeo=14,x-systemd.idle-timeout=1min 0  0" >> /etc/fstab
) 1> /root/post_install-step2.log 2>&1

