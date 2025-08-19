from commands import SayHelloCommand, LoginCommand, CreateUserCommand, LogoutCommand, \
    RefreshTokenCommand, MeCommand, UploadUserAvatarCommand, GenerateSecureDownloadLinkCommand, AddRoleCommand, \
    AssignRoleToUserCommand, GetCommandNamesCommand, SyncPermissionCommand


from commands.listing.all import GetAllListingsCommand
from commands.listing.create import CreateListingCommand
from commands.listing.update import UpdateListingCommand
from commands.listing.delete import DeleteListingCommand
from commands.listing.select import SelectListingCommand
from commands.listing.pictures.uploads import UploadListingPicturesCommand
from commands.listing.pictures.set_primary import SetPrimaryListingPictureCommand
from commands.listing.pictures.delete import DeleteListingPicturesCommand

command_registry = [
    CreateUserCommand(),
    LoginCommand(),
    SayHelloCommand(),
    RefreshTokenCommand(),
    LogoutCommand(),
    MeCommand(),
    UploadUserAvatarCommand(),
    GenerateSecureDownloadLinkCommand(),
    AddRoleCommand(),
    AssignRoleToUserCommand(),
    GetCommandNamesCommand(),
    SyncPermissionCommand()
    ,
    # listing
    GetAllListingsCommand(),
    CreateListingCommand(),
    UpdateListingCommand(),
    DeleteListingCommand(),
    SelectListingCommand(),
    UploadListingPicturesCommand(),
    SetPrimaryListingPictureCommand(),
    DeleteListingPicturesCommand(),
]


