# split script
script is going to need way too many helpers; split it into two scripts like better-initramfs
`tools.sh`
`phase.sh`

# please pipefail
please, for everyone's sake


# gum integration
 - more interactive uis using gum

# naming
let the user follow their own scheme but strictify it: alphanumeric, lowercase, hyphens only

# templating
no more individual template sizes; template sizes (specifically, CPU and memory) are defined in config first

# command naming
some commands are just qm wrappers, but a few names changed:
 - labctl vm create   -> phase vm provision
 - labctl vm connect  -> phase vm shell


